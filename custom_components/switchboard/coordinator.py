"""State coordinator: initial REST snapshot + a long-lived events websocket.

Push integration. `_async_update_data` runs once (initial `/api/state` + `/api/connections`);
after that a background task streams `/api/events/ws`, patches the snapshot in place, and calls
`async_set_updated_data` so entities update. Every frame is also re-fired on the HA bus as
`switchboard_event` for user automations.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SwitchboardApiError, SwitchboardAuthError, SwitchboardClient
from .const import (
    DOMAIN,
    EVENT_SWITCHBOARD,
    SPOTIFY_PAUSED,
    SPOTIFY_PLAYING,
    SPOTIFY_STOPPED,
)

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff ladder (seconds): never stops retrying, capped at 60s. Reset to step 0 on a
# successful connect. Matches the Switchboard app's own app→HA ladder.
RECONNECT_LADDER = (1, 2, 3, 5, 10, 15, 30, 45, 60)


@dataclass
class SwitchboardData:
    """Normalised, entity-friendly view of a Switchboard machine snapshot."""

    obs: dict[str, dict[str, Any]] = field(default_factory=dict)  # connection_id -> fields
    spotify: str = SPOTIFY_STOPPED
    spotify_now: dict[str, Any] | None = None
    afk: bool = False
    twitch: dict[str, dict[str, Any]] = field(default_factory=dict)  # connection_id -> live data
    version: str = ""
    update: dict[str, Any] | None = None  # {version, body, ready} or None
    # App-detection: the focused app id (or None) + whether any watched app is focused/running.
    focused_app: str | None = None
    watched_app_active: bool = False


_TWITCH_KEYS = (
    "label",
    "live",
    "viewers",
    "chatters",
    "title",
    "category_id",
    "category_name",
    "box_art_url",
    "started_at_ms",
)


def _state_from_snapshot(raw: dict[str, Any]) -> SwitchboardData:
    obs: dict[str, dict[str, Any]] = {}
    for o in raw.get("obs", []):
        obs[o["id"]] = {
            "label": o.get("label", o["id"]),
            "connected": o.get("connected", False),
            "streaming": o.get("streaming", False),
            "recording": o.get("recording", False),
            "current_scene": o.get("current_scene"),
        }
    twitch: dict[str, dict[str, Any]] = {}
    for tw in raw.get("twitch", []):
        twitch[tw["id"]] = {k: tw.get(k) for k in _TWITCH_KEYS}
    apps = raw.get("apps") or {}
    return SwitchboardData(
        obs=obs,
        spotify=raw.get("spotify", SPOTIFY_STOPPED),
        spotify_now=raw.get("spotify_now"),
        afk=raw.get("afk", False),
        twitch=twitch,
        version=raw.get("version", ""),
        update=raw.get("update"),
        focused_app=apps.get("focused"),
        watched_app_active=bool(apps.get("watched_focused") or apps.get("watched_running")),
    )


class SwitchboardCoordinator(DataUpdateCoordinator[SwitchboardData]):
    """Owns the client, the snapshot, and the events websocket task."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SwitchboardClient,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.entry = entry
        self.client = client
        self.connections: list[dict[str, Any]] = []
        self._closing = False
        self._ws_task: asyncio.Task[None] | None = None
        self._refreshing = False
        self._refresh_again = False

    async def _async_update_data(self) -> SwitchboardData:
        try:
            self.connections = await self.client.fetch_connections()
            raw = await self.client.fetch_state()
        except SwitchboardApiError as err:
            raise UpdateFailed(str(err)) from err
        return _state_from_snapshot(raw)

    async def async_start(self) -> None:
        """Launch the events websocket as a background task."""
        self._ws_task = self.hass.async_create_background_task(
            self._ws_loop(), f"{DOMAIN}_events_ws"
        )

    async def async_stop(self) -> None:
        """Stop the websocket task (called on entry unload)."""
        self._closing = True
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None

    # --- connection lookup (used by services) ------------------------------------------------

    def obs_ids(self) -> set[str]:
        return {c["id"] for c in self.connections if c["integration"] == "obs"}

    def twitch_ids(self) -> set[str]:
        return {c["id"] for c in self.connections if c["integration"] == "twitch"}

    def connection_label(self, connection_id: str) -> str:
        for c in self.connections:
            if c["id"] == connection_id:
                return c.get("label", connection_id)
        return connection_id

    def resolve_connection_id(self, target: str, integration: str | None = None) -> str | None:
        """Accept either a raw id or a friendly label; return the id.

        Returns None when nothing matches (caller decides how to fall back) and raises
        ValueError when a label matches MORE than one connection — the two cases must stay
        distinguishable, or an ambiguous label silently degrades into a bogus id.
        """
        for c in self.connections:
            if c["id"] == target:
                return c["id"]
        matches = [
            c
            for c in self.connections
            if c["label"] == target and (integration is None or c["integration"] == integration)
        ]
        if len(matches) == 1:
            return matches[0]["id"]
        if not matches:
            return None
        raise ValueError(f"'{target}' is ambiguous — use the connection id")

    # --- events websocket --------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        step = 0
        while not self._closing:
            try:
                async with self.client.ws_connect() as ws:
                    _LOGGER.debug("switchboard: events websocket connected")
                    step = 0
                    # On EVERY connect (first included), re-fetch the snapshot: the event stream
                    # only carries *changes*, so anything that happened between setup's snapshot
                    # (or a disconnect) and this socket opening was missed and our state is stale.
                    await self._resync()
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                self._handle_frame(msg.json())
                            except Exception:  # one bad frame must not kill the loop
                                _LOGGER.exception("switchboard: failed to handle event frame")
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                # A revoked/rotated token surfaces here as a 401 on the upgrade request.
                if err.status == 401:
                    self._start_reauth()
                else:
                    _LOGGER.debug("switchboard: events websocket handshake failed: %s", err)
            except SwitchboardAuthError:
                # The resync REST calls got a 401 — same revoked-token case.
                self._start_reauth()
            except (TimeoutError, aiohttp.ClientError, SwitchboardApiError) as err:
                _LOGGER.debug("switchboard: events websocket dropped: %s", err)
            except Exception:  # this task must never die; entities freeze if it does
                _LOGGER.exception("switchboard: unexpected error in events loop")
            if self._closing:
                break
            # Disconnected → entities go unavailable instead of freezing at stale values
            # ("streaming: on" hours after the machine shut down). Restored by the resync above.
            if self.last_update_success:
                self.last_update_success = False
                self.async_update_listeners()
            await asyncio.sleep(RECONNECT_LADDER[step])
            step = min(step + 1, len(RECONNECT_LADDER) - 1)

    def _start_reauth(self) -> None:
        """Kick off the reauth flow (deduped by HA) — the token no longer works."""
        _LOGGER.warning("switchboard: API token rejected — starting reauthentication")
        self.entry.async_start_reauth(self.hass)

    async def _resync(self) -> None:
        """Re-fetch connections + the full snapshot and replace self.data — run on every ws
        connect to recover anything missed while disconnected. Raises on failure so the caller
        drops the connection and retries with backoff (a connected socket patching a stale
        snapshot is worse than a reconnect).
        """
        self.connections = await self.client.fetch_connections()
        raw = await self.client.fetch_state()
        self.async_set_updated_data(_state_from_snapshot(raw))

    @callback
    def _handle_frame(self, frame: dict[str, Any]) -> None:
        # Re-fire raw frame for user automations regardless of whether it touches an entity.
        self.hass.bus.async_fire(EVENT_SWITCHBOARD, frame)
        if self._apply(frame):
            self.async_set_updated_data(self.data)

    @callback
    def _apply(self, frame: dict[str, Any]) -> bool:
        """Patch self.data from one event frame. Returns True if entity state changed."""
        data = self.data
        etype = frame.get("type")

        if etype == "connections_changed":
            # Connection set may have changed → refresh list + snapshot, reload if entities differ.
            self.hass.async_create_task(self._refresh_connections())
            return False

        cid = frame.get("connection_id")
        if etype in ("obs_scene_changed", "obs_connection", "obs_stream_state", "obs_record_state"):
            inst = data.obs.get(cid)
            if inst is None:
                return False  # unknown connection; a connections_changed/reload will add it
            if etype == "obs_scene_changed":
                inst["current_scene"] = frame.get("scene")
            elif etype == "obs_connection":
                inst["connected"] = bool(frame.get("connected"))
            elif etype == "obs_stream_state":
                inst["streaming"] = bool(frame.get("active"))
            elif etype == "obs_record_state":
                inst["recording"] = bool(frame.get("active"))
            return True

        if etype == "machine_state_changed":
            data.afk = frame.get("state") == "afk"
            return True

        if etype == "app_detect_changed":
            data.focused_app = frame.get("focused")
            data.watched_app_active = bool(
                frame.get("watched_focused") or frame.get("watched_running")
            )
            return True

        if etype == "twitch_stream_status":
            inst = data.twitch.setdefault(cid, {})
            for k in (
                "live",
                "title",
                "category_id",
                "category_name",
                "box_art_url",
                "started_at_ms",
            ):
                inst[k] = frame.get(k)
            return True
        if etype == "twitch_chatters_updated":
            inst = data.twitch.setdefault(cid, {})
            inst["viewers"] = frame.get("watching")
            inst["chatters"] = frame.get("chatters")
            return True

        if etype == "update_available":
            data.update = {
                "version": frame.get("version"),
                "body": frame.get("body"),
                "ready": False,
            }
            return True
        if etype == "update_ready":
            # Carry forward any previously-shown body; update_ready only signals readiness.
            data.update = {
                **(data.update or {}),
                "version": frame.get("version"),
                "ready": True,
            }
            return True

        if etype in (
            "spotify_song_changed",
            "spotify_playlist_changed",
            "spotify_playback_started",
            "spotify_now_playing",
        ):
            now = frame.get("now")
            data.spotify_now = now
            # `now is None` means nothing is playing (per the app's SpotifyNowPlaying contract) →
            # the gate is "stopped", not "paused". A present-but-not-playing track is paused.
            if now is None:
                data.spotify = SPOTIFY_STOPPED
            else:
                data.spotify = SPOTIFY_PLAYING if now.get("playing") else SPOTIFY_PAUSED
            return True
        if etype == "spotify_playback_paused":
            data.spotify_now = frame.get("now")
            data.spotify = SPOTIFY_PAUSED
            return True
        if etype == "spotify_playback_stopped":
            data.spotify_now = None
            data.spotify = SPOTIFY_STOPPED
            return True

        return False

    def _entity_shape(self) -> set[tuple[str, str]]:
        """(id, label) of every connection that backs entities — labels included so a rename
        (which feeds device names) triggers a rebuild, not just an id-set change."""
        return {
            (c["id"], c.get("label", c["id"]))
            for c in self.connections
            if c["integration"] in ("obs", "twitch")
        }

    async def _refresh_connections(self) -> None:
        if self._refreshing:
            # A refresh is already in flight — flag it to run once more so a connections_changed
            # that lands AFTER its fetch resolved isn't dropped. Avoids overlapping async_reload
            # calls racing the same entry when events arrive in bursts.
            self._refresh_again = True
            return
        self._refreshing = True
        try:
            while True:
                self._refresh_again = False
                try:
                    new_conns = await self.client.fetch_connections()
                except SwitchboardApiError as err:
                    _LOGGER.debug("switchboard: connection refresh failed: %s", err)
                    return
                before = self._entity_shape()
                self.connections = new_conns
                if before != self._entity_shape():
                    # Added/removed/renamed OBS or Twitch connection → entities and device names
                    # must be (re)built; reload the entry (which also cancels this task's owner).
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(self.entry.entry_id)
                    )
                    return
                if not self._refresh_again:
                    return
        finally:
            self._refreshing = False

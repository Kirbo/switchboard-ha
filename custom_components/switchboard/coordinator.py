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
import time
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    SwitchboardAccessError,
    SwitchboardApiError,
    SwitchboardAuthError,
    SwitchboardClient,
)
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

# How long an /api/afk resample waits before firing, so a burst of scene changes costs one
# request instead of one per event.
AFK_COALESCE_SECS = 1.0

# Events that patch one OBS instance (all keyed by `connection_id`).
_OBS_EVENTS = (
    "obs_scene_changed",
    "obs_connection",
    "obs_stream_state",
    "obs_record_state",
    "obs_scenes_changed",
    "obs_delay_changed",
    "stream_delay_changed",
)

# Events carrying a full NowPlaying payload for a track/context change.
_SPOTIFY_TRACK_EVENTS = (
    "spotify_song_changed",
    "spotify_playlist_changed",
    "spotify_playback_started",
    "spotify_now_playing",
)


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
    # App-detection: the focused app id (or None), the running watch-list ids, and the two
    # separate "a watched app is …" flags the contract distinguishes.
    focused_app: str | None = None
    running_apps: list[str] = field(default_factory=list)
    watched_focused: bool = False
    watched_running: bool = False
    # AFK numbers sampled from GET /api/afk. Only the two that are STABLE between samples are
    # kept: `threshold_secs` (soonest idle→AFK rule threshold, scene override applied; None =
    # nothing will auto-set AFK) and the snooze window turned into an ABSOLUTE deadline so a
    # stale sample can't lie about how much is left. `idle_secs`/`afk_in_secs` change every
    # second by design and are deliberately not mirrored — poll /api/afk for a live countdown.
    afk_threshold_secs: int | None = None
    afk_snooze_until_ms: int | None = None

    @property
    def watched_app_active(self) -> bool:
        """A watched app is in play (foreground OR running)."""
        return self.watched_focused or self.watched_running


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

# The now-playing fields we surface. `position_ms`/`updated_at_ms` are deliberately dropped:
# `spotify_now_playing` is re-emitted on position drift, so mirroring them would rewrite the
# sensor (and its recorder history) every few seconds for no visible change.
_SPOTIFY_KEYS = (
    "playing",
    "title",
    "artist",
    "featuring",
    "album",
    "playlist",
    "playlist_url",
    "art_url",
    "url",
    "up_next_title",
    "up_next_artist",
    "duration_ms",
)


def _spotify_view(now: dict[str, Any] | None) -> dict[str, Any] | None:
    """Trim a NowPlaying payload to the fields we expose (see `_SPOTIFY_KEYS`)."""
    if not now:
        return None
    return {k: now.get(k) for k in _SPOTIFY_KEYS}


def _spotify_gate(now: dict[str, Any] | None) -> str:
    """`now is None` means nothing is playing (the app's SpotifyNowPlaying contract) → the gate
    is "stopped", not "paused". A present-but-not-playing track is paused."""
    if not now:
        return SPOTIFY_STOPPED
    return SPOTIFY_PLAYING if now.get("playing") else SPOTIFY_PAUSED


def _obs_from_snapshot(o: dict[str, Any]) -> dict[str, Any]:
    """One `ApiState.obs[]` entry → our per-instance fields. `scenes` has no snapshot field
    (only the `obs_scenes_changed` event carries it), so it starts empty and fills in live."""
    return {
        "label": o.get("label", o["id"]),
        "connected": o.get("connected", False),
        "streaming": o.get("streaming", False),
        "recording": o.get("recording", False),
        "current_scene": o.get("current_scene"),
        "stream_started_ms": o.get("stream_started_ms"),
        "stream_delay_secs": o.get("stream_delay_secs"),
        "scenes": [],
    }


def _apply_afk(data: SwitchboardData, raw: dict[str, Any]) -> bool:
    """Fold a `GET /api/afk` payload into `data`. Returns True when something we expose moved.

    `snooze_secs` is a countdown, so it is stored as an absolute deadline: a sample taken two
    minutes ago then still reads correctly instead of claiming a window that already elapsed.
    """
    threshold = raw.get("threshold_secs")
    threshold = None if threshold is None else int(threshold)
    snooze = raw.get("snooze_secs")
    until = None if snooze is None else int(time.time() * 1000) + int(snooze) * 1000

    changed = threshold != data.afk_threshold_secs
    data.afk_threshold_secs = threshold

    was = data.afk_snooze_until_ms
    if (until is None) != (was is None):
        data.afk_snooze_until_ms = until
        changed = True
    elif until is not None and was is not None and abs(until - was) > 2000:
        # Deadlines drift by the request round-trip; only a >2 s move is a real re-snooze, so
        # resampling an unchanged window doesn't rewrite the entity.
        data.afk_snooze_until_ms = until
        changed = True
    return changed


def _state_from_snapshot(
    raw: dict[str, Any], previous: SwitchboardData | None = None
) -> SwitchboardData:
    obs: dict[str, dict[str, Any]] = {}
    for o in raw.get("obs", []):
        inst = _obs_from_snapshot(o)
        # Carry the live-only scene list across a resync so it doesn't blank on every reconnect.
        if previous and (old := previous.obs.get(o["id"])):
            inst["scenes"] = old.get("scenes") or []
        obs[o["id"]] = inst
    twitch: dict[str, dict[str, Any]] = {}
    for tw in raw.get("twitch", []):
        twitch[tw["id"]] = {k: tw.get(k) for k in _TWITCH_KEYS}
    apps = raw.get("apps") or {}
    return SwitchboardData(
        obs=obs,
        spotify=raw.get("spotify", SPOTIFY_STOPPED),
        spotify_now=_spotify_view(raw.get("spotify_now")),
        # `afk` and `machine_state` mirror each other server-side; prefer the explicit string
        # when present so the two can never be read inconsistently.
        afk=(raw["machine_state"] == "afk") if "machine_state" in raw else raw.get("afk", False),
        twitch=twitch,
        version=raw.get("version", ""),
        update=raw.get("update"),
        focused_app=apps.get("focused"),
        running_apps=list(apps.get("running") or []),
        watched_focused=bool(apps.get("watched_focused")),
        watched_running=bool(apps.get("watched_running")),
        # Preserved across a resync — /api/state doesn't carry them, /api/afk does.
        afk_threshold_secs=previous.afk_threshold_secs if previous else None,
        afk_snooze_until_ms=previous.afk_snooze_until_ms if previous else None,
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
        self._afk_task: asyncio.Task[None] | None = None
        self._afk_again = False
        # Once-per-outage log gates — reset after a fully successful connect (ws + resync).
        self._access_denied_logged = False
        self._reauth_logged = False

    async def _async_update_data(self) -> SwitchboardData:
        try:
            self.connections = await self.client.fetch_connections()
            raw = await self.client.fetch_state()
        except SwitchboardApiError as err:
            raise UpdateFailed(str(err)) from err
        data = _state_from_snapshot(raw, self.data)
        try:
            _apply_afk(data, await self.client.fetch_afk())
        except SwitchboardApiError as err:
            # Never fail setup on the AFK extras — they are attributes on an entity whose main
            # state came from /api/state, which already succeeded.
            _LOGGER.debug("switchboard: /api/afk unavailable: %s", err)
        return data

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
        if self._afk_task:
            self._afk_task.cancel()
            self._afk_task = None

    # --- connection lookup (used by services) ------------------------------------------------

    def _ids(self, integration: str) -> set[str]:
        return {c["id"] for c in self.connections if c.get("integration") == integration}

    def obs_ids(self) -> set[str]:
        return self._ids("obs")

    def twitch_ids(self) -> set[str]:
        return self._ids("twitch")

    def ha_ids(self) -> set[str]:
        """Downstream Home Assistant connections *of the Switchboard app* — the target of the
        `ha_service_call` / `ha_light_flash` actions (not this integration's own HA)."""
        return self._ids("home_assistant")

    def connection_label(self, connection_id: str) -> str:
        for c in self.connections:
            if c["id"] == connection_id:
                return c.get("label", connection_id)
        return connection_id

    def default_id(self, integration: str) -> str | None:
        """The default connection for an integration — or the only one, which the app treats as
        the implicit default. None when there are several and none is flagged."""
        candidates = [c for c in self.connections if c.get("integration") == integration]
        if len(candidates) == 1:
            return candidates[0]["id"]
        for c in candidates:
            if c.get("is_default"):
                return c["id"]
        return None

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
            if c.get("label") == target
            and (integration is None or c.get("integration") == integration)
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
                    self._access_denied_logged = False
                    self._reauth_logged = False
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
                elif err.status == 403:
                    self._log_access_denied()
                else:
                    _LOGGER.debug("switchboard: events websocket handshake failed: %s", err)
            except SwitchboardAuthError:
                # The resync REST calls got a 401 — same revoked-token case.
                self._start_reauth()
            except SwitchboardAccessError:
                # The resync REST calls got a 403 — the token is fine but an ACL denies us.
                self._log_access_denied()
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
        """Kick off the reauth flow (deduped by HA) — the token no longer works.

        The log line is gated once per outage; HA dedups the flow itself, but the retry loop
        would repeat the warning every backoff step while the reauth sits unresolved.
        """
        if not self._reauth_logged:
            self._reauth_logged = True
            _LOGGER.warning("switchboard: API token rejected — starting reauthentication")
        self.entry.async_start_reauth(self.hass)

    def _log_access_denied(self) -> None:
        """403: the token is valid but this caller is denied by an ACL. Warn once per outage —
        the retry loop would otherwise repeat it every backoff step."""
        if self._access_denied_logged:
            return
        self._access_denied_logged = True
        _LOGGER.warning(
            "switchboard: access denied (403) — check the Events ACL / allowlist under "
            "Settings → External API; entities stay unavailable until access is restored"
        )

    async def _resync(self) -> None:
        """Re-fetch connections + the full snapshot and replace self.data — run on every ws
        connect to recover anything missed while disconnected. Raises on failure so the caller
        drops the connection and retries with backoff (a connected socket patching a stale
        snapshot is worse than a reconnect). If the connection set changed shape while we were
        down, this also schedules the entry reload that rebuilds the entities.
        """
        new_conns = await self.client.fetch_connections()
        raw = await self.client.fetch_state()
        data = _state_from_snapshot(raw, self.data)
        try:
            _apply_afk(data, await self.client.fetch_afk())
        except SwitchboardApiError as err:
            # Best-effort extras: a missing /api/afk must not force a reconnect loop when the
            # snapshot itself came through fine.
            _LOGGER.debug("switchboard: /api/afk unavailable: %s", err)
        self._replace_connections(new_conns)
        self.async_set_updated_data(data)

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

        # `connections_changed`, `update_available` and `update_ready` are classified INTERNAL in
        # the app (events.rs `ha_contract::INTERNAL_EVENTS`) — the bus ships them to every
        # consumer, but the contract doesn't guarantee them. We use them as live *hints* only:
        # every value they touch is also carried by /api/state, which is re-fetched on every
        # reconnect, so dropping them would cost freshness, never correctness.
        if etype == "connections_changed":
            # Connection set may have changed → refresh list + snapshot, reload if entities differ.
            self.hass.async_create_task(self._refresh_connections())
            return False

        cid = frame.get("connection_id")
        if etype in _OBS_EVENTS:
            inst = data.obs.get(cid)
            if inst is None:
                return False  # unknown connection; a connections_changed/reload will add it
            if etype == "obs_scene_changed":
                inst["current_scene"] = frame.get("scene")
                # A per-scene AFK override on the default instance's live scene REPLACES the
                # rule threshold while that scene is up (docs/HA.md "AFK numbers") — resample.
                self.schedule_afk_refresh()
            elif etype == "obs_connection":
                inst["connected"] = bool(frame.get("connected"))
            elif etype == "obs_stream_state":
                active = bool(frame.get("active"))
                # The event carries no start timestamp; derive it on the off→on edge (the next
                # resync replaces it with the authoritative `stream_started_ms`).
                if active and not inst.get("streaming"):
                    inst["stream_started_ms"] = int(time.time() * 1000)
                elif not active:
                    inst["stream_started_ms"] = None
                inst["streaming"] = active
            elif etype == "obs_record_state":
                inst["recording"] = bool(frame.get("active"))
            elif etype == "obs_scenes_changed":
                inst["scenes"] = list(frame.get("scenes") or [])
            elif etype == "obs_delay_changed":
                inst["stream_delay_secs"] = frame.get("secs")  # None = delay off
            elif etype == "stream_delay_changed":
                # A scene change flipped the delay on this instance.
                inst["stream_delay_secs"] = frame.get("seconds") if frame.get("enabled") else None
            return True

        if etype == "machine_state_changed":
            data.afk = frame.get("state") == "afk"
            # An explicit active/afk flip can consume or moot a snooze window — resample.
            self.schedule_afk_refresh()
            return True

        if etype == "app_detect_changed":
            data.focused_app = frame.get("focused")
            data.running_apps = list(frame.get("running") or [])
            data.watched_focused = bool(frame.get("watched_focused"))
            data.watched_running = bool(frame.get("watched_running"))
            return True

        if etype == "twitch_category_updated":
            inst = data.twitch.setdefault(cid, {})
            inst["category_id"] = frame.get("game_id")
            inst["category_name"] = frame.get("game_name")
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

        if etype in _SPOTIFY_TRACK_EVENTS:
            return self._set_spotify(frame.get("now"), _spotify_gate(frame.get("now")))
        if etype == "spotify_playback_paused":
            return self._set_spotify(frame.get("now"), SPOTIFY_PAUSED)
        if etype == "spotify_playback_stopped":
            # No payload on this one (docs/HA.md field notes) — nothing is playing.
            return self._set_spotify(None, SPOTIFY_STOPPED)

        # Everything else on the contract's event list (home_assistant_*, overlay_alert,
        # peer_lifecycle/peer_reachability, rule_fired, the rule_action_* trio,
        # rule_events_dropped, external_command, opendeck_*, twitch_event, twitch_go_live,
        # twitch_stream_target_restored, navigate_to_view, obs_reconnecting, obs_scene_renamed,
        # obs_launched_local, obs_stream_health, twitch_chat_command, mesh_identity_reset,
        # plugin_paired/removed,
        # spotify_song_liked/spotify_playlist_track_added, insights_session_ended)
        # backs no entity — it is already on the HA bus as `switchboard_event` for automations.
        #
        # The two Spotify like/playlist events deliberately DON'T touch the Spotify sensor: they
        # carry no now-playing snapshot (the liked track may already have moved on by the time the
        # frame arrives), and they are momentary actions rather than state. Automate on them with
        # `event_type: switchboard_event` + `event_data.type`, branching on the `liked`/`added`
        # boolean, which carries the direction.
        #
        # obs_stream_health is event-only for now: it IS a persisting state, but /api/state carries
        # no health field, so an entity would have nothing to hydrate from after a restart and
        # would sit at an invented value until the next verdict change. Automate on the event.
        #
        # insights_session_ended likewise backs no entity: it is a one-shot summary of a session
        # that has just ENDED, not current state, so a sensor holding it would report stale numbers
        # for however long until the next stream. Its whole payload rides the bus event, which is
        # what an automation wants (post a recap, log the numbers, drive a notification).
        return False

    @callback
    def _set_spotify(self, now: dict[str, Any] | None, gate: str) -> bool:
        """Store a now-playing payload + gate. Returns True only when something we EXPOSE
        changed: `spotify_now_playing` is re-emitted on position drift, so patching blindly
        would rewrite the sensor (and its history) every few seconds for no visible change."""
        view = _spotify_view(now)
        if view == self.data.spotify_now and gate == self.data.spotify:
            return False
        self.data.spotify_now = view
        self.data.spotify = gate
        return True

    @callback
    def schedule_afk_refresh(self) -> None:
        """Resample `GET /api/afk` soon, coalescing bursts into one request.

        Called when something that can move `threshold_secs`/`snooze_secs` happened (scene
        change, machine-state flip, one of our own AFK service calls). There is no clock: the
        live `idle_secs` countdown is not mirrored (see `SwitchboardData`).
        """
        if self._closing:
            return
        if self._afk_task and not self._afk_task.done():
            self._afk_again = True
            return
        self._afk_task = self.hass.async_create_task(self._refresh_afk())

    async def _refresh_afk(self) -> None:
        while True:
            self._afk_again = False
            await asyncio.sleep(AFK_COALESCE_SECS)
            if self._closing:
                return
            try:
                raw = await self.client.fetch_afk()
            except SwitchboardApiError as err:
                _LOGGER.debug("switchboard: /api/afk refresh failed: %s", err)
                return
            if _apply_afk(self.data, raw):
                self.async_set_updated_data(self.data)
            if not self._afk_again:
                return

    def _entity_shape(self) -> set[tuple[str, str]]:
        """(id, label) of every connection that backs entities — labels included so a rename
        (which feeds device names) triggers a rebuild, not just an id-set change."""
        return {
            (c["id"], c.get("label", c["id"]))
            for c in self.connections
            if c["integration"] in ("obs", "twitch")
        }

    def _replace_connections(self, new_conns: list[dict[str, Any]]) -> bool:
        """Swap in a fresh connection list; if the entity-backing shape changed
        (added/removed/renamed OBS or Twitch connection), entities and device names must be
        (re)built — schedule an entry reload. Returns True when a reload was scheduled.
        """
        before = self._entity_shape()
        self.connections = new_conns
        if before == self._entity_shape():
            return False
        self.hass.async_create_task(self.hass.config_entries.async_reload(self.entry.entry_id))
        return True

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
                if self._replace_connections(new_conns):
                    # Entry reload scheduled (which also cancels this task's owner).
                    return
                if not self._refresh_again:
                    return
        finally:
            self._refreshing = False

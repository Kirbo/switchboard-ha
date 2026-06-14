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

from .api import SwitchboardApiError, SwitchboardClient
from .const import (
    DOMAIN,
    EVENT_SWITCHBOARD,
    SPOTIFY_PAUSED,
    SPOTIFY_PLAYING,
    SPOTIFY_STOPPED,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class SwitchboardData:
    """Normalised, entity-friendly view of a Switchboard machine snapshot."""

    obs: dict[str, dict[str, Any]] = field(default_factory=dict)  # connection_id -> fields
    spotify: str = SPOTIFY_STOPPED
    spotify_now: dict[str, Any] | None = None
    afk: bool = False


def _state_from_snapshot(raw: dict[str, Any]) -> SwitchboardData:
    obs: dict[str, dict[str, Any]] = {}
    for o in raw.get("obs", []):
        obs[o["id"]] = {
            "label": o.get("label", o["id"]),
            "connected": o.get("connected", False),
            "streaming": o.get("streaming", False),
            "recording": o.get("recording", False),
            "current_scene": o.get("current_scene"),
            "stream_started_ms": o.get("stream_started_ms"),
        }
    return SwitchboardData(
        obs=obs,
        spotify=raw.get("spotify", SPOTIFY_STOPPED),
        afk=raw.get("afk", False),
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

    def resolve_connection_id(self, target: str, integration: str | None = None) -> str:
        """Accept either a raw id or a friendly label; return the id. Raises on ambiguity."""
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
            raise ValueError(f"no connection matches '{target}'")
        raise ValueError(f"'{target}' is ambiguous — use the connection id")

    # --- events websocket --------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        backoff = 1
        while not self._closing:
            try:
                async with self.client.ws_connect() as ws:
                    _LOGGER.debug("switchboard: events websocket connected")
                    backoff = 1
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_frame(msg.json())
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except (TimeoutError, aiohttp.ClientError, SwitchboardApiError) as err:
                _LOGGER.debug("switchboard: events websocket dropped: %s", err)
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

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

        if etype == "afk_changed":
            data.afk = bool(frame.get("active"))
            return True

        if etype in ("spotify_song_changed", "spotify_playback_started", "spotify_now_playing"):
            now = frame.get("now")
            data.spotify_now = now
            data.spotify = SPOTIFY_PLAYING if (now and now.get("playing")) else SPOTIFY_PAUSED
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

    async def _refresh_connections(self) -> None:
        try:
            new_conns = await self.client.fetch_connections()
        except SwitchboardApiError as err:
            _LOGGER.debug("switchboard: connection refresh failed: %s", err)
            return
        before = self.obs_ids()
        self.connections = new_conns
        after = self.obs_ids()
        if before != after:
            # New/removed OBS instance → entities must be (re)built; reload the entry.
            self.hass.async_create_task(self.hass.config_entries.async_reload(self.entry.entry_id))

"""The app's `resync` frame, and frames that arrive while a snapshot read is out.

docs/HA.md "resync" (app 2026-10-13): the app sends `{"type": "resync"}` when this socket fell
behind its bus and events were dropped — change-only state would stay stale until it moved again,
so the consumer re-reads /api/state. Frames that arrive while that read is in flight are newer
than the snapshot it returns, so they must be applied OVER it, in arrival order: an older snapshot
must never overwrite a newer event. These tests drive the real `_ws_loop` with a socket the fake
app keeps pushing onto while a state read is out.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.switchboard import coordinator as coord_mod
from custom_components.switchboard.const import DOMAIN, EVENT_SWITCHBOARD
from custom_components.switchboard.coordinator import SwitchboardCoordinator

from .conftest import FakeClient
from .test_contract import API_AFK, API_STATE, CID, RESYNC_FRAME as RESYNC
from .test_coordinator import CONNECTIONS


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coord_mod, "RECONNECT_LADDER", (0,))
    monkeypatch.setattr(coord_mod, "AFK_COALESCE_SECS", 0)


class _Msg:
    type = aiohttp.WSMsgType.TEXT

    def __init__(self, frame: dict[str, Any]) -> None:
        self._frame = frame

    def json(self) -> dict[str, Any]:
        return self._frame


def _scene(scene: str) -> dict[str, Any]:
    return {"type": "obs_scene_changed", "connection_id": CID, "scene": scene}


class FramesClient(FakeClient):
    """One socket session. Every `/api/state` read returns the OLD snapshot (scene "Gaming") and,
    while it is "in flight", the app pushes the frames scripted for that read onto the socket. It
    yields to the event loop meanwhile, so a re-read that did NOT hold those frames back would
    apply them first and then lose them under the snapshot."""

    def __init__(
        self, frames: list[dict[str, Any]], during_reads: list[list[dict[str, Any]]]
    ) -> None:
        super().__init__(state=API_STATE, connections=list(CONNECTIONS), afk=API_AFK)
        self.queue = list(frames)
        self.during_reads = during_reads
        self.reads = 0
        self.coord: SwitchboardCoordinator | None = None

    async def fetch_state(self) -> dict[str, Any]:
        if self.reads < len(self.during_reads):
            self.queue.extend(self.during_reads[self.reads])
        self.reads += 1
        for _ in range(5):
            await asyncio.sleep(0)
        return API_STATE

    def ws_connect(self, resume: Any = None) -> Any:
        client = self

        class _Ws:
            def __aiter__(self) -> _Ws:
                return self

            async def __anext__(self) -> Any:
                if not client.queue:
                    assert client.coord is not None
                    client.coord._closing = True  # one session, then the loop stops
                    raise StopAsyncIteration
                return _Msg(client.queue.pop(0))

        @asynccontextmanager
        async def _cm():
            yield _Ws()

        return _cm()


async def _run(
    hass: HomeAssistant,
    frames: list[dict[str, Any]],
    during_reads: list[list[dict[str, Any]]],
) -> tuple[SwitchboardCoordinator, FramesClient, list[dict[str, Any]]]:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client = FramesClient(frames, during_reads)
    coord = SwitchboardCoordinator(hass, entry, client)  # type: ignore[arg-type]
    client.coord = coord
    coord.connections = list(CONNECTIONS)
    coord.data = coord_mod._state_from_snapshot(API_STATE)
    fired: list[dict[str, Any]] = []
    hass.bus.async_listen(EVENT_SWITCHBOARD, lambda e: fired.append(dict(e.data)))
    await coord._ws_loop()
    await hass.async_block_till_done()
    return coord, client, fired


async def test_a_resync_frame_re_reads_the_state(hass: HomeAssistant) -> None:
    """The scene drifted on a frame we got; then the app says we missed some. The re-read must
    happen (one on connect + one for the frame) and its snapshot is adopted."""
    coord, client, fired = await _run(hass, [_scene("Drifted"), RESYNC], [])
    assert client.reads == 2, "connect + the resync frame"
    assert coord.data.obs[CID]["current_scene"] == "Gaming", "the fresh snapshot is adopted"
    assert [f["type"] for f in fired] == ["obs_scene_changed"], (
        "resync concerns this socket only — it is not re-fired on the HA bus"
    )


async def test_an_event_during_the_resync_read_survives_the_older_snapshot(
    hass: HomeAssistant,
) -> None:
    """While the resync's /api/state read is out the scene moves twice: the last move wins."""
    coord, client, _fired = await _run(hass, [RESYNC], [[], [_scene("Live"), _scene("BRB")]])
    assert client.reads == 2
    assert coord.data.obs[CID]["current_scene"] == "BRB"


async def test_an_event_during_the_connect_read_survives_the_older_snapshot(
    hass: HomeAssistant,
) -> None:
    """Same rule for the read every connect makes before streaming."""
    coord, client, _fired = await _run(hass, [], [[_scene("Live")]])
    assert client.reads == 1
    assert coord.data.obs[CID]["current_scene"] == "Live"


def _discord_connected() -> dict[str, Any]:
    return {
        "type": "connection_status_changed",
        "entry": {
            "key": "discord/rpc/local",
            "integration": "discord",
            "component": "rpc",
            "connection_id": None,
            "subject": "local",
            "label": None,
            "state": "connected",
            "since_ms": 1760800000000,
            "last_error": None,
            "last_error_ms": None,
            "drops": 0,
            "reconnects": 0,
            "crashes": 0,
            "queued": None,
            "counted": True,
        },
    }


def _discord_mute(mute: bool) -> dict[str, Any]:
    return {
        "type": "discord_voice_settings_changed",
        "mute": mute,
        "deaf": False,
        "input_mode": "voice_activity",
        "previous_mute": not mute,
        "previous_deaf": False,
        "previous_input_mode": "voice_activity",
    }


async def test_the_discord_session_connecting_re_reads_the_state(hass: HomeAssistant) -> None:
    """docs/HA.md "Discord voice": a reconnect's first reading is a baseline the app sends as no
    event, so the row turning `connected` must re-read /api/state (connect + this one)."""
    coord, client, fired = await _run(hass, [_discord_mute(True), _discord_connected()], [])
    assert client.reads == 2, "connect + the Discord session connecting"
    assert coord.data.discord_voice is not None
    assert coord.data.discord_voice["mute"] is False, "the fresh snapshot is adopted"
    assert [f["type"] for f in fired] == [
        "discord_voice_settings_changed",
        "connection_status_changed",
    ]


async def test_a_discord_frame_during_that_read_survives_the_older_snapshot(
    hass: HomeAssistant,
) -> None:
    """While that re-read is out, Discord is muted: the mute wins over the older snapshot."""
    coord, client, _fired = await _run(hass, [_discord_connected()], [[], [_discord_mute(True)]])
    assert client.reads == 2
    assert coord.data.discord_voice is not None
    assert coord.data.discord_voice["mute"] is True

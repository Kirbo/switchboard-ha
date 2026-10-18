"""Push over poll: the snapshot is a (re)connect seed, the event stream keeps it current.

docs/HA.md (the app's contract): fetch `/api/state` on every (re)connect, then stay live from
`/api/events/ws` — never poll. These tests prove it two ways:

- behaviourally, by driving the real coordinator + `_ws_loop` with a fake app and HA's own clock
  (`async_fire_time_changed`) — hours of simulated time read nothing, a pushed frame moves state
  without a read, a dropped socket backs off on the ladder and re-seeds exactly once per connect, a
  malformed frame is dropped, a 401 stops the loop and starts reauth (no retry storm), a 403 backs
  off;
- statically, with an AST scan of the integration that fails on any HA time tracker, a polling
  `update_interval`, a sleep-and-read loop outside the two pinned ones, or a new caller of a read.

Deterministic: the reconnect wait is an injected recorder (`_backoff_sleep`), the AFK resample's
coalescing window is zeroed, and the live socket hands frames over through a queue — no sleeps.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import aiohttp
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.switchboard import coordinator as coord_mod
from custom_components.switchboard.api import SwitchboardAuthError
from custom_components.switchboard.const import DOMAIN, EVENT_SWITCHBOARD
from custom_components.switchboard.coordinator import SwitchboardCoordinator

from .conftest import FakeClient
from .test_contract import API_AFK, API_STATE, CID
from .test_coordinator import CONNECTIONS

SRC = Path(__file__).resolve().parent.parent / "custom_components" / "switchboard"


@pytest.fixture(autouse=True)
def _no_afk_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coord_mod, "AFK_COALESCE_SECS", 0)


class _Msg:
    def __init__(self, raw: str, kind: aiohttp.WSMsgType = aiohttp.WSMsgType.TEXT) -> None:
        self.type = kind
        self._raw = raw

    def json(self) -> Any:
        return json.loads(self._raw)  # ValueError on a non-JSON frame, like aiohttp's


def _frame(frame: Any) -> _Msg:
    return _Msg(json.dumps(frame))


def _scene(scene: str) -> _Msg:
    return _frame({"type": "obs_scene_changed", "connection_id": CID, "scene": scene})


class _Session:
    """One socket session: the scripted frames, then a clean close."""

    def __init__(self, frames: list[_Msg]) -> None:
        self.frames = list(frames)

    def __aiter__(self) -> _Session:
        return self

    async def __anext__(self) -> _Msg:
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)


class _LiveSession:
    """A socket that stays open; the test pushes frames and waits until each was handled."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[_Msg] = asyncio.Queue()
        self.idle = asyncio.Event()  # set while the loop waits for the next frame

    def __aiter__(self) -> _LiveSession:
        return self

    async def __anext__(self) -> _Msg:
        if self.queue.empty():
            self.idle.set()
        msg = await self.queue.get()
        self.idle.clear()
        return msg

    async def push(self, msg: _Msg) -> None:
        self.idle.clear()
        self.queue.put_nowait(msg)
        async with asyncio.timeout(5):  # a guard against a hang, never a wait for time to pass
            await self.idle.wait()


class PushClient(FakeClient):
    """Counts every read and follows a script of `ws_connect` attempts: an exception to raise on
    enter, or a session to yield. The loop is told to stop after the last scripted attempt."""

    def __init__(self, script: list[Exception | _Session | _LiveSession]) -> None:
        super().__init__(state=API_STATE, connections=list(CONNECTIONS), afk=API_AFK)
        self.script = script
        self.attempts = 0
        self.reads = {"state": 0, "connections": 0, "afk": 0}
        self.rest_error: Exception | None = None
        self.coord: SwitchboardCoordinator | None = None
        # The resume position each attempt asked for (None = a plain connect).
        self.resumes: list[Any] = []

    async def fetch_state(self) -> dict[str, Any]:
        self.reads["state"] += 1
        return await super().fetch_state()

    async def fetch_connections(self) -> list[dict[str, Any]]:
        self.reads["connections"] += 1
        if self.rest_error is not None:
            raise self.rest_error
        return await super().fetch_connections()

    async def fetch_afk(self) -> dict[str, Any]:
        self.reads["afk"] += 1
        return await super().fetch_afk()

    def ws_connect(self, resume: Any = None) -> Any:
        self.resumes.append(resume)
        step = self.script[self.attempts]
        self.attempts += 1
        if self.attempts >= len(self.script):
            assert self.coord is not None
            self.coord._closing = True

        @asynccontextmanager
        async def _cm():
            if isinstance(step, Exception):
                raise step
            yield step

        return _cm()


def _make(
    hass: HomeAssistant, script: list[Exception | _Session | _LiveSession]
) -> tuple[SwitchboardCoordinator, PushClient, MockConfigEntry, list[tuple[float, bool, Any]]]:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "192.0.2.10"})
    entry.add_to_hass(hass)
    client = PushClient(script)
    coord = SwitchboardCoordinator(hass, entry, client)  # type: ignore[arg-type]
    client.coord = coord
    coord.connections = list(CONNECTIONS)
    coord.data = coord_mod._state_from_snapshot(API_STATE)
    waits: list[tuple[float, bool, Any]] = []

    async def _record(delay: float) -> None:
        # (the wait, whether entities were still marked available, the scene the mirror held)
        waits.append((delay, coord.last_update_success, coord.data.obs[CID]["current_scene"]))

    coord._backoff_sleep = _record
    coord._jitter = lambda: 0.5  # the jitter's midpoint: every wait is exactly its ladder step
    return coord, client, entry, waits


def _handshake(status: int) -> aiohttp.WSServerHandshakeError:
    return aiohttp.WSServerHandshakeError(MagicMock(), (), status=status)


def _reauth_flows(hass: HomeAssistant, entry: MockConfigEntry) -> list[Any]:
    return list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


# --- behaviour --------------------------------------------------------------------------------


async def test_no_clock_reads_the_app_only_the_seed_and_events_do(hass: HomeAssistant) -> None:
    """Setup's refresh + the connect re-seed are the only reads; hours of HA time add none, and a
    pushed frame moves the state without any /api/state read."""
    live = _LiveSession()
    coord, client, _entry, _waits = _make(hass, [live, _Session([])])
    assert coord.update_interval is None, "a DataUpdateCoordinator interval would be a poll"

    await coord.async_refresh()  # what async_config_entry_first_refresh runs at setup
    unsub = coord.async_add_listener(lambda: None)  # an entity is listening, as in real use
    await coord.async_start()
    task = coord._ws_task
    try:
        async with asyncio.timeout(5):
            await live.idle.wait()  # connected, re-seeded, waiting for frames
        assert client.reads == {"state": 2, "connections": 2, "afk": 2}, "setup + connect seed"

        for hours in (1, 6, 24):
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=hours))
            await hass.async_block_till_done()
        assert client.reads == {"state": 2, "connections": 2, "afk": 2}, "time alone read nothing"

        await live.push(_scene("Live"))
        await hass.async_block_till_done()
        assert coord.data.obs[CID]["current_scene"] == "Live", "the pushed frame moved the state"
        assert client.reads["state"] == 2, "no /api/state read for a pushed change"
        assert client.reads["connections"] == 2
        # A scene change can move the AFK threshold (a per-scene override), so it resamples
        # /api/afk ONCE — triggered by the event, never by a clock.
        assert client.reads["afk"] == 3

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=48))
        await hass.async_block_till_done()
        assert client.reads == {"state": 2, "connections": 2, "afk": 3}
    finally:
        unsub()
        await coord.async_stop()
        assert task is not None
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_drop_backs_off_on_the_ladder_and_re_seeds_once_per_connect(
    hass: HomeAssistant,
) -> None:
    refused = aiohttp.ClientConnectionError("refused")
    coord, client, _entry, waits = _make(
        hass,
        [
            _Session([_scene("Live")]),
            refused,  # the app is restarting
            refused,
            _Session([_scene("BRB")]),
            _Session([]),
        ],
    )
    await coord._ws_loop()
    await hass.async_block_till_done()

    assert client.attempts == 5
    assert [w[0] for w in waits] == [1, 2, 3, 1], "1-2-3 while refused; reset by the reconnect"
    assert not any(w[1] for w in waits), "entities are unavailable while disconnected"
    assert [w[2] for w in waits] == ["Live", "Live", "Live", "BRB"], (
        "the mirror keeps its last pushed value while down; the frames after a re-seed apply on top"
    )
    # One seed per SUCCESSFUL connect (sessions 1, 4, 5) — a refused attempt reads nothing.
    assert client.reads["state"] == 3
    assert client.reads["connections"] == 3


async def test_a_malformed_or_unknown_frame_is_dropped_and_the_stream_survives(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    unknown = {"type": "from_a_newer_app", "anything": [1, 2, 3]}
    coord, client, _entry, waits = _make(
        hass,
        [
            _Session(
                [
                    _Msg("not json {"),
                    _Msg("null"),
                    _Msg("42"),
                    _Msg('"a string"'),
                    _Msg("[]"),
                    _Msg("\x00", aiohttp.WSMsgType.BINARY),
                    _frame({"no_type": True}),
                    _frame(unknown),
                    _frame({"type": "obs_scene_changed"}),  # no connection_id
                    _frame({"type": "obs_connection", "connection_id": "not-a-known-id"}),
                    _scene("After"),
                ]
            )
        ],
    )
    fired: list[dict[str, Any]] = []
    hass.bus.async_listen(EVENT_SWITCHBOARD, lambda e: fired.append(dict(e.data)))
    caplog.set_level(logging.DEBUG, logger="custom_components.switchboard")

    await coord._ws_loop()
    await hass.async_block_till_done()

    assert coord.data.obs[CID]["current_scene"] == "After", "the frame after the junk still applied"
    assert client.reads["state"] == 1, "the socket never dropped: no re-seed"
    assert waits == [], "no reconnect"
    assert unknown in fired, "an unknown type is still re-fired for automations (additive schema)"
    assert [f.get("type") for f in fired] == [
        None,
        "from_a_newer_app",
        "obs_scene_changed",
        "obs_connection",
        "obs_scene_changed",
    ], "exactly the object frames reach the bus — never a bare null/number/list/string"
    handled = [r for r in caplog.records if "failed to handle event frame" in r.getMessage()]
    assert handled == [], "no frame needed the catch-all: each was dropped deliberately"


async def test_a_401_on_the_upgrade_starts_reauth_and_stops_retrying(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """docs/HA.md: a 401 means the credential is gone — not a transient error. The loop must not
    keep dialling a dead token (each attempt is an auth failure the app logs and throttles)."""
    caplog.set_level(logging.WARNING, logger="custom_components.switchboard")
    coord, client, entry, waits = _make(hass, [_handshake(401), _Session([]), _Session([])])
    await coord._ws_loop()
    await hass.async_block_till_done()

    assert client.attempts == 1, "one attempt, then the loop stops"
    assert waits == [], "no backoff ladder either — nothing to wait for"
    assert client.reads["state"] == 0
    assert coord.last_update_success is False, "entities go unavailable"
    assert len(_reauth_flows(hass, entry)) == 1, "the user is asked for a new token"
    rejected = [r for r in caplog.records if "token rejected" in r.getMessage()]
    assert len(rejected) == 1


async def test_a_seed_that_keeps_failing_keeps_backing_off(hass: HomeAssistant) -> None:
    """The ladder resets on a SYNCED connect, not on the bare socket open: an app that accepts the
    socket but fails every seed read (a 500 while it starts) is not redialled once a second."""
    coord, client, _entry, waits = _make(hass, [_Session([]) for _ in range(5)])
    client.rest_error = coord_mod.SwitchboardApiError("GET /api/connections -> HTTP 500")
    await coord._ws_loop()
    await hass.async_block_till_done()
    assert client.attempts == 5
    assert [w[0] for w in waits] == [1, 2, 3, 5], "the ladder climbs while the seed fails"


async def test_a_dead_socket_the_heartbeat_caught_reconnects_and_re_seeds(
    hass: HomeAssistant,
) -> None:
    """A link that died without a FIN (the app's PC slept) is closed by aiohttp's heartbeat when
    its ping goes unanswered, and surfaces as an ERROR message: that is a drop like any other —
    entities go unavailable, the loop backs off and the next connect re-seeds."""
    heartbeat_timeout = _Msg("", aiohttp.WSMsgType.ERROR)
    coord, client, _entry, waits = _make(
        hass,
        [
            _Session([_scene("Live"), heartbeat_timeout, _scene("never")]),
            _Session([_scene("Back")]),
        ],
    )
    await coord._ws_loop()
    await hass.async_block_till_done()
    assert [w[0] for w in waits] == [1]
    assert waits[0][1] is False, "unavailable while down"
    assert waits[0][2] == "Live", "nothing after the ERROR was applied"
    assert coord.data.obs[CID]["current_scene"] == "Back"
    assert client.reads["state"] == 2, "re-seeded on the reconnect"
    assert coord.last_update_success is True


async def test_each_wait_is_jittered_around_its_ladder_step(hass: HomeAssistant) -> None:
    refused = aiohttp.ClientConnectionError("refused")
    coord, _client, _entry, waits = _make(hass, [refused, refused, refused, _Session([])])
    draws = iter([0.0, 1.0, 0.5])
    coord._jitter = lambda: next(draws)
    await coord._ws_loop()
    assert [round(w[0], 6) for w in waits] == [0.8, 2.4, 3.0], "±20 % around 1, 2, 3"


async def test_the_ladder_caps_and_never_gives_up(hass: HomeAssistant) -> None:
    refused = aiohttp.ClientConnectionError("refused")
    coord, client, _entry, waits = _make(hass, [refused] * 12 + [_Session([])])
    await coord._ws_loop()
    assert client.attempts == 13
    assert [w[0] for w in waits] == [1, 2, 3, 5, 10, 15, 30, 45, 60, 60, 60, 60]


async def test_a_401_on_the_resync_read_stops_retrying_too(hass: HomeAssistant) -> None:
    coord, client, entry, waits = _make(hass, [_Session([_scene("Live")]), _Session([])])
    client.rest_error = SwitchboardAuthError("invalid or missing API token")
    await coord._ws_loop()
    await hass.async_block_till_done()

    assert client.attempts == 1
    assert waits == []
    assert client.reads["state"] == 0, "the connections read failed first"
    assert coord.data.obs[CID]["current_scene"] != "Live", "no frame is applied over a failed seed"
    assert len(_reauth_flows(hass, entry)) == 1


async def test_a_403_keeps_backing_off_instead_of_stopping(hass: HomeAssistant) -> None:
    """403 is an ACL the operator may re-grant from the app — the integration cannot be told when,
    so it keeps trying on the ladder (never in a tight loop), and asks for no new token."""
    coord, client, entry, waits = _make(hass, [_handshake(403), _handshake(403), _Session([])])
    await coord._ws_loop()
    await hass.async_block_till_done()

    assert client.attempts == 3
    assert [w[0] for w in waits] == [1, 2]
    assert client.reads["state"] == 1, "one seed, on the connect that got through"
    assert _reauth_flows(hass, entry) == []


# --- static scan ------------------------------------------------------------------------------

# HA helpers that run something on a clock. None may appear: every read is a seed or an event.
_TIME_TRACKERS = {
    "async_track_time_interval",
    "async_track_point_in_time",
    "async_track_point_in_utc_time",
    "async_track_time_change",
    "async_track_utc_time_change",
    "async_call_later",
    "call_later",
    "call_at",
}
# Everything that reads the app (directly, or the seed that does).
_READS = {"fetch_state", "fetch_connections", "fetch_afk", "_get", "_get_once", "_resync"}


def _modules() -> dict[str, ast.Module]:
    return {p.name: ast.parse(p.read_text(), filename=str(p)) for p in sorted(SRC.glob("*.py"))}


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]


def _called(node: ast.AST) -> set[str]:
    """Names of everything called under `node` (`x.y()` → "y", `f()` → "f")."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


def _callers(name: str) -> set[str]:
    """`module.function` of every function that calls `name`."""
    return {
        f"{mod}:{fn.name}"
        for mod, tree in _modules().items()
        for fn in _functions(tree)
        if name in _called(fn)
        # A nested function is also walked as part of its parent — attribute it to the innermost.
        and not any(
            name in _called(inner)
            for inner in _functions(ast.Module(body=fn.body, type_ignores=[]))
        )
    }


def test_the_scan_sees_the_integration() -> None:
    mods = _modules()
    assert {"coordinator.py", "api.py", "sensor.py"} <= set(mods), sorted(mods)
    assert _callers("fetch_state"), "a scan that finds no read would pass vacuously"


def test_no_time_tracker_or_polling_interval_exists() -> None:
    found: list[str] = []
    intervals: list[ast.keyword] = []
    for mod, tree in _modules().items():
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and _called(n) & _TIME_TRACKERS:
                found.append(f"{mod}:{n.lineno} {sorted(_called(n) & _TIME_TRACKERS)}")
            if isinstance(n, ast.keyword) and n.arg == "update_interval":
                intervals.append(n)
            if isinstance(n, ast.Assign | ast.AnnAssign):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    tname = t.id if isinstance(t, ast.Name) else getattr(t, "attr", "")
                    if tname == "SCAN_INTERVAL":
                        found.append(f"{mod}:{n.lineno} SCAN_INTERVAL")
                    if tname in ("should_poll", "_attr_should_poll") and not (
                        isinstance(n.value, ast.Constant) and n.value.value is False
                    ):
                        found.append(f"{mod}:{n.lineno} {tname}")
    assert found == [], "a clock-driven read: mirror the pushed event instead (docs/HA.md)"
    assert intervals, "the coordinator's update_interval should be seen"
    for kw in intervals:
        assert isinstance(kw.value, ast.Constant) and kw.value.value is None, (
            f"update_interval={ast.unparse(kw.value)} polls the app"
        )


def test_only_the_pinned_loops_wait_and_read() -> None:
    """A `while` that both sleeps and reads is what a poller looks like. Two exist, neither a clock:
    the reconnect loop (it reads once per successful connect — the behaviour tests above) and the
    AFK resample, which exits unless a NEW event re-armed it while it ran."""
    loops: dict[str, ast.While] = {}
    for mod, tree in _modules().items():
        for fn in _functions(tree):
            for w in (n for n in ast.walk(fn) if isinstance(n, ast.While)):
                calls = _called(w)
                if calls & {"sleep", "_backoff_sleep"} and calls & _READS:
                    loops[f"{mod}:{fn.name}"] = w
    assert set(loops) == {"coordinator.py:_ws_loop", "coordinator.py:_refresh_afk"}, sorted(loops)

    afk = loops["coordinator.py:_refresh_afk"]
    gate = [
        n
        for n in ast.walk(afk)
        if isinstance(n, ast.If)
        and "_afk_again" in ast.unparse(n.test)
        and any(isinstance(b, ast.Return) for b in n.body)
    ]
    assert gate, "the AFK resample must return unless an event re-armed it (`_afk_again`)"


def test_every_wait_is_a_backoff_or_a_coalesce() -> None:
    sleeps = _callers("sleep")
    assert sleeps == {"coordinator.py:_refresh_afk", "api.py:_retrying"}, sorted(sleeps)
    assert _callers("_backoff_sleep") == {"coordinator.py:_ws_loop"}


def test_reads_happen_only_at_the_seed_or_on_an_event() -> None:
    """Pin who reads what. A new caller is a new read path — check it is a seed or event-driven,
    not a timer, and add it here."""
    assert _callers("fetch_state") == {
        "coordinator.py:_async_update_data",  # setup's first refresh (update_interval=None)
        "coordinator.py:_resync",  # every connect + the app's `resync` frame
        "config_flow.py:_async_validate",  # the user submitting the config / reauth form
    }
    assert _callers("_resync") == {"coordinator.py:_ws_loop"}
    assert _callers("fetch_connections") == {
        "coordinator.py:_async_update_data",
        "coordinator.py:_resync",
        "coordinator.py:_refresh_connections",  # the `connections_changed` event
    }
    assert _callers("_refresh_connections") == {"coordinator.py:_apply"}
    assert _callers("fetch_afk") == {
        "coordinator.py:_async_update_data",
        "coordinator.py:_resync",
        "coordinator.py:_refresh_afk",
    }
    assert _callers("_refresh_afk") == {"coordinator.py:schedule_afk_refresh"}
    # The resample is armed by an event (`_apply`) or by one of our own AFK service calls.
    assert _callers("schedule_afk_refresh") == {
        "coordinator.py:_apply",
        "services.py:handle_set_machine_state",
        "services.py:handle_afk_snooze",
        "services.py:handle_afk_reset_idle",
    }

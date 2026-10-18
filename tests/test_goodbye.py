"""The goodbye frame on Home Assistant's stop (docs/HA.md, `goodbye`).

When HA stops, the integration tells the app over its open events socket —
`{"type": "goodbye", "reason": "restart" | "stop"}` — so the app treats the drop of its own HA
connection (and of this socket) as an expected restart rather than an outage. These tests pin:
the stop listener is registered once per entry and removed on unload; the frame and its reason;
that it is written synchronously inside the event fire (HA cancels the events loop just before
firing EVENT_HOMEASSISTANT_STOP, so a later send would find the socket closed); that a missing
socket or a stalled send never blocks or raises; and that the events loop stays quiet afterwards.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from typing import Any
from unittest.mock import patch

from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    EVENT_CALL_SERVICE,
    EVENT_HOMEASSISTANT_STOP,
    RESTART_EXIT_CODE,
)
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.switchboard import coordinator as coord_mod
from custom_components.switchboard.const import CONF_FINGERPRINT, DOMAIN
from custom_components.switchboard.coordinator import SwitchboardCoordinator

from .conftest import FakeClient
from .test_contract import API_AFK, API_STATE
from .test_coordinator import CONNECTIONS

DATA = {
    CONF_HOST: "192.0.2.10",
    CONF_PORT: 38474,
    CONF_TOKEN: "test-token",
    CONF_VERIFY_SSL: False,
    CONF_FINGERPRINT: "ab" * 32,
}


class FakeWs:
    """An open events socket that records what is sent on it."""

    def __init__(self, *, hang: bool = False, error: Exception | None = None) -> None:
        self.sent: list[Any] = []
        self.closed = False
        self.hang = hang
        self.error = error

    async def send_json(self, data: Any) -> None:
        # Recorded BEFORE any suspension, as aiohttp writes an uncompressed frame to the transport
        # synchronously — that is what lets the frame beat the events loop's close.
        if self.error is not None:
            raise self.error
        self.sent.append(data)
        if self.hang:
            await asyncio.Event().wait()


def _coordinator(hass: HomeAssistant, ws: FakeWs | None) -> SwitchboardCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client = FakeClient(state=API_STATE, connections=list(CONNECTIONS), afk=API_AFK)
    coord = SwitchboardCoordinator(hass, entry, client)  # type: ignore[arg-type]
    coord._ws = ws  # type: ignore[assignment]
    coord.async_listen_ha_stop()
    return coord


def _stop_listeners(hass: HomeAssistant) -> int:
    """How many of OUR goodbye listeners are on the stop event (HA and other components add
    their own, so the raw count is not ours to assert on)."""
    return sum(
        1
        for job, _filter in hass.bus._listeners.get(EVENT_HOMEASSISTANT_STOP, [])
        if getattr(
            getattr(getattr(job.target, "listener_job", None), "target", None), "__name__", None
        )
        == "_async_ha_stopping"
    )


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, title="Switchboard (test)")
    entry.add_to_hass(hass)
    client = FakeClient(state=API_STATE, connections=list(CONNECTIONS), afk=API_AFK)
    with (
        patch("custom_components.switchboard.SwitchboardClient", return_value=client),
        patch(
            "custom_components.switchboard.coordinator.SwitchboardCoordinator.async_start",
            return_value=None,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_the_stop_listener_is_registered_once_and_removed_on_unload(
    hass: HomeAssistant,
) -> None:
    baseline = _stop_listeners(hass)
    entry = await _setup(hass)
    assert _stop_listeners(hass) == baseline + 1

    client = FakeClient(state=API_STATE, connections=list(CONNECTIONS), afk=API_AFK)
    with (
        patch("custom_components.switchboard.SwitchboardClient", return_value=client),
        patch(
            "custom_components.switchboard.coordinator.SwitchboardCoordinator.async_start",
            return_value=None,
        ),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert _stop_listeners(hass) == baseline + 1, "a reloaded entry would say goodbye twice"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _stop_listeners(hass) == baseline, "the unloaded entry still listens for the stop"


async def test_a_plain_stop_says_goodbye_with_reason_stop(hass: HomeAssistant) -> None:
    ws = FakeWs()
    coord = _coordinator(hass, ws)
    hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
    # No await in between: the frame is on the socket synchronously, inside the event fire.
    assert ws.sent == [{"type": "goodbye", "reason": "stop"}]
    assert coord._closing, "the events loop must stop reconnecting once HA is going down"
    await hass.async_block_till_done()
    assert len(ws.sent) == 1


async def test_a_restart_exit_code_says_restart(hass: HomeAssistant) -> None:
    ws = FakeWs()
    _coordinator(hass, ws)
    hass.exit_code = RESTART_EXIT_CODE
    hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
    assert ws.sent == [{"type": "goodbye", "reason": "restart"}]


async def test_a_restart_service_call_says_restart(hass: HomeAssistant) -> None:
    """On a Supervisor install the restart service has the Supervisor restart the container,
    which stops Core with SIGTERM (exit code 0). The service call is what marks it a restart."""
    ws = FakeWs()
    _coordinator(hass, ws)
    hass.bus.async_fire_internal(
        EVENT_CALL_SERVICE, {"domain": "homeassistant", "service": "restart", "service_data": {}}
    )
    hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
    assert ws.sent == [{"type": "goodbye", "reason": "restart"}]


async def test_a_stale_restart_request_does_not_label_a_later_stop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart refused by HA's config check leaves no stop behind; a stop much later is a stop."""
    ws = FakeWs()
    coord = _coordinator(hass, ws)
    now = [1000.0]
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: now[0])
    hass.bus.async_fire_internal(
        EVENT_CALL_SERVICE, {"domain": "homeassistant", "service": "restart", "service_data": {}}
    )
    assert coord._restart_requested_at == 1000.0
    now[0] += coord_mod.RESTART_REQUEST_TTL_SECS + 1
    hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
    assert ws.sent == [{"type": "goodbye", "reason": "stop"}]


async def test_other_service_calls_are_not_a_restart(hass: HomeAssistant) -> None:
    ws = FakeWs()
    _coordinator(hass, ws)
    hass.bus.async_fire_internal(
        EVENT_CALL_SERVICE, {"domain": "homeassistant", "service": "reload_all", "service_data": {}}
    )
    hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
    assert ws.sent == [{"type": "goodbye", "reason": "stop"}]


@pytest.mark.parametrize("ws", [None, "closed"])
async def test_no_open_socket_is_a_silent_no_op(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, ws: str | None
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.switchboard")
    sock = None
    if ws == "closed":
        sock = FakeWs()
        sock.closed = True
    coord = _coordinator(hass, sock)
    hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert sock is None or sock.sent == []
    assert coord._closing
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("custom_components.switchboard")
    ]
    assert any("no goodbye sent" in r.message for r in caplog.records)


async def test_a_failing_send_is_swallowed(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.switchboard")
    _coordinator(hass, FakeWs(error=ConnectionResetError("Cannot write to closing transport")))
    hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("goodbye not sent" in r.message for r in caplog.records)


async def test_a_stalled_send_is_cut_off_by_the_timeout(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never block HA's shutdown: a socket that will not drain is abandoned after the timeout."""
    monkeypatch.setattr(coord_mod, "GOODBYE_TIMEOUT_SECS", 0.01)
    ws = FakeWs(hang=True)
    _coordinator(hass, ws)
    hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
    async with asyncio.timeout(5):  # a guard against a hang, not a timing assertion
        await hass.async_block_till_done()
    assert ws.sent == [{"type": "goodbye", "reason": "stop"}]


async def test_unload_after_the_stop_fired_does_not_error(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The fired once-listener is already gone; removing it again would log an exception."""
    coord = _coordinator(hass, FakeWs())
    hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    coord.async_unlisten_ha_stop()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


class _StopMidSession:
    """A session that HA's stop interrupts: the goodbye fires, then the socket breaks oddly."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.closed = False
        self.sent: list[Any] = []

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)

    def __aiter__(self) -> _StopMidSession:
        return self

    async def __anext__(self) -> Any:
        self.hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
        raise RuntimeError("socket torn down by the shutdown")


async def test_the_events_loop_is_quiet_and_stops_after_the_goodbye(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.switchboard")
    ws = _StopMidSession(hass)
    attempts = 0

    class _Client(FakeClient):
        def ws_connect(self, resume: Any = None) -> Any:
            nonlocal attempts
            attempts += 1

            @asynccontextmanager
            async def _cm():
                yield ws

            return _cm()

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client = _Client(state=API_STATE, connections=list(CONNECTIONS), afk=API_AFK)
    coord = SwitchboardCoordinator(hass, entry, client)  # type: ignore[arg-type]
    coord.async_listen_ha_stop()
    async with asyncio.timeout(5):  # the loop must END, not retry
        await coord._ws_loop()
    await hass.async_block_till_done()

    assert ws.sent == [{"type": "goodbye", "reason": "stop"}]
    assert attempts == 1, "the loop reconnected after HA said it was stopping"
    assert coord._ws is None
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("custom_components.switchboard")
    ]

"""The events-websocket reconnect loop: what a TLS fingerprint mismatch does (SB-E-033).

`_ws_loop` used to catch `aiohttp.ClientError` — which `ServerFingerprintMismatch` is — and log
it at DEBUG, so after a legitimate identity rotation the integration flipped unavailable and
retried every 60 s forever with nothing visible, and an impersonation attempt on the LAN left no
trace at all. These tests drive the real loop with a client whose `ws_connect` raises the
mismatch, and check that it is warned about ONCE per outage, surfaced as a repairs issue, and
that the issue is retired by the next successful connect — without the pin ever being adopted.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.switchboard import coordinator as coord_mod
from custom_components.switchboard.const import DOMAIN, ISSUE_FINGERPRINT_MISMATCH
from custom_components.switchboard.coordinator import SwitchboardCoordinator

from .conftest import FakeClient
from .test_contract import API_AFK, API_STATE
from .test_coordinator import CONNECTIONS

PINNED = bytes.fromhex("ab" * 32)
SEEN = bytes.fromhex("cd" * 32)


def _mismatch() -> aiohttp.ServerFingerprintMismatch:
    return aiohttp.ServerFingerprintMismatch(PINNED, SEEN, "192.0.2.10", 38474)


class _EmptyWs:
    """A websocket that opens and closes immediately with no frames."""

    def __aiter__(self) -> _EmptyWs:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration


class ScriptedClient(FakeClient):
    """`ws_connect` follows a script, one entry per attempt: an exception to raise on enter, or
    None for a clean (empty) session. The loop is told to stop after the last entry."""

    def __init__(self, script: list[Exception | None]) -> None:
        super().__init__(state=API_STATE, connections=list(CONNECTIONS), afk=API_AFK)
        self.script = script
        self.attempts = 0
        self.coord: SwitchboardCoordinator | None = None

    def ws_connect(self) -> Any:
        step = self.script[self.attempts]
        self.attempts += 1
        if self.attempts >= len(self.script):
            assert self.coord is not None
            self.coord._closing = True

        @asynccontextmanager
        async def _cm():
            if step is not None:
                raise step
            yield _EmptyWs()

        return _cm()


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coord_mod, "RECONNECT_LADDER", (0,))


async def _run_loop(
    hass: HomeAssistant, script: list[Exception | None]
) -> tuple[SwitchboardCoordinator, MockConfigEntry]:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client = ScriptedClient(script)
    coord = SwitchboardCoordinator(hass, entry, client)  # type: ignore[arg-type]
    client.coord = coord
    coord.connections = list(CONNECTIONS)
    coord.data = coord_mod._state_from_snapshot(API_STATE)
    await coord._ws_loop()
    await hass.async_block_till_done()
    assert client.attempts == len(script)
    return coord, entry


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_FINGERPRINT_MISMATCH}_{entry.entry_id}"
    )


async def test_a_fingerprint_mismatch_is_warned_once_per_outage(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Three consecutive mismatches: one WARNING carrying the digest the host presented, so the
    user can compare it against the Peers tab — not one per backoff step, and not DEBUG."""
    caplog.set_level(logging.DEBUG, logger="custom_components.switchboard")
    await _run_loop(hass, [_mismatch(), _mismatch(), _mismatch()])

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "fingerprint" in r.message
    ]
    assert len(warnings) == 1, [r.message for r in warnings]
    assert SEEN.hex() in warnings[0].message, "the seen digest is what the user compares"
    assert "192.0.2.10" in warnings[0].message


async def test_a_fingerprint_mismatch_raises_a_repairs_issue(
    hass: HomeAssistant,
) -> None:
    coord, entry = await _run_loop(hass, [_mismatch()])
    issue = _issue(hass, entry)
    assert issue is not None, "a DEBUG line nobody has enabled is not a signal"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == ISSUE_FINGERPRINT_MISMATCH
    assert issue.translation_placeholders == {"host": "192.0.2.10", "seen_fingerprint": SEEN.hex()}
    assert issue.is_fixable is False, "the fix is a human pasting a fingerprint they confirmed"
    # The pin is NEVER adopted: nothing on the coordinator or the entry changed.
    assert coord.client.attempts == 1
    assert entry.data == {}


async def test_the_issue_is_cleared_by_the_next_successful_connect(hass: HomeAssistant) -> None:
    """Mismatch, then a clean connect (the user reconfigured, or the impostor went away): the
    issue must not outlive the problem, and the once-per-outage gate must re-arm so a SECOND
    outage is warned about again."""
    coord, entry = await _run_loop(hass, [_mismatch(), None])
    assert _issue(hass, entry) is None, "the repairs issue outlived the problem"
    assert coord._fingerprint_mismatch_logged is False, "the warn-once gate must re-arm"


async def test_the_issue_is_re_raised_on_a_second_outage(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="custom_components.switchboard")
    _coord, entry = await _run_loop(hass, [_mismatch(), None, _mismatch()])
    assert _issue(hass, entry) is not None
    warnings = [r for r in caplog.records if "fingerprint" in r.message]
    assert len(warnings) == 2, "one per outage: the second outage is a new event"


async def test_a_generic_client_error_stays_at_debug(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The mismatch arm must not widen: an ordinary connection refused (the app is simply not
    running) is still the quiet, expected DEBUG path and raises no issue."""
    caplog.set_level(logging.DEBUG, logger="custom_components.switchboard")
    _coord, entry = await _run_loop(hass, [aiohttp.ClientConnectionError("refused")])
    assert _issue(hass, entry) is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

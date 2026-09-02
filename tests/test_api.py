"""The API client's HTTP-status handling — chiefly the write budget (HTTP 429).

docs/HA.md "Write budget": every authenticated caller may write 10 times a second (burst 30)
across `POST /api/command`, `POST /api/event` and the websocket equivalents. Over that the app
answers `429 too many commands — slow down`. That is a "later", not a "no" and not a "broken":
the budget refills continuously, so the client must wait and re-send. The two failures this file
exists to prevent are retrying immediately (which keeps the budget spent) and surfacing the 429
as a dead connection or a bad token (which would take every entity unavailable, or start a
pointless reauth flow).
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.switchboard import api as api_mod
from custom_components.switchboard.api import (
    SwitchboardApiError,
    SwitchboardAuthError,
    SwitchboardClient,
    SwitchboardRateLimitError,
)


class _Resp:
    def __init__(self, status: int, payload: Any = None, text: str = "") -> None:
        self.status = status
        self._payload = {} if payload is None else payload
        self._text = text

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return self._text


class _Ctx:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    async def __aenter__(self) -> _Resp:
        return self._resp

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class FakeSession:
    """Just enough `aiohttp.ClientSession` for `SwitchboardClient`.

    Responses are served in order; the LAST one repeats forever, so `[_Resp(429)]` models an app
    that stays over budget.
    """

    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def _next(self) -> _Ctx:
        self.calls += 1
        resp = self._responses[0] if len(self._responses) == 1 else self._responses.pop(0)
        return _Ctx(resp)

    def get(self, *_a: object, **_k: object) -> _Ctx:
        return self._next()

    def post(self, *_a: object, **_k: object) -> _Ctx:
        return self._next()


@pytest.fixture(autouse=True)
def _no_backoff_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same number of attempts, no wall-clock wait."""
    monkeypatch.setattr(api_mod, "RATE_LIMIT_BACKOFF", (0, 0, 0))


def _client(session: FakeSession) -> SwitchboardClient:
    return SwitchboardClient(
        session,  # type: ignore[arg-type]
        "192.0.2.10",
        38474,
        "test-token",
        verify_ssl=False,
        fingerprint="ab" * 32,
    )


async def test_a_throttled_command_is_re_sent_and_succeeds() -> None:
    session = FakeSession([_Resp(429), _Resp(429), _Resp(200, {"ok": True, "acted": True})])
    assert await _client(session).send_command({"action_type": "afk_reset_idle"}) == {
        "ok": True,
        "acted": True,
    }
    assert session.calls == 3


async def test_a_command_that_stays_throttled_gives_up_after_the_ladder() -> None:
    session = FakeSession([_Resp(429)])
    with pytest.raises(SwitchboardRateLimitError):
        await _client(session).send_command({"action_type": "afk_reset_idle"})
    # One attempt per backoff step, plus the final one — bounded, never an endless retry loop.
    assert session.calls == len(api_mod.RATE_LIMIT_BACKOFF) + 1


async def test_a_rate_limit_is_still_an_api_error() -> None:
    """Subclassing matters: every existing `except SwitchboardApiError` keeps handling a 429 as
    the transient failure it is, so no caller had to be taught about the new class."""
    assert issubclass(SwitchboardRateLimitError, SwitchboardApiError)
    assert not issubclass(SwitchboardRateLimitError, SwitchboardAuthError)


async def test_a_rejected_token_is_not_retried() -> None:
    """401 is a "no" — re-sending cannot mint a valid token, and the retries are what trip the
    app's failed-auth throttle."""
    session = FakeSession([_Resp(401)])
    with pytest.raises(SwitchboardAuthError):
        await _client(session).send_command({"action_type": "afk_reset_idle"})
    assert session.calls == 1


async def test_a_throttled_read_is_re_sent_rather_than_read_as_an_outage() -> None:
    """A 429 on a GET must not propagate: in the coordinator it would drop the events websocket
    and mark every entity unavailable, which is exactly what "slow down" does not mean."""
    session = FakeSession([_Resp(429), _Resp(200, {"obs": []})])
    assert await _client(session).fetch_state() == {"obs": []}
    assert session.calls == 2


async def test_other_http_errors_are_not_retried() -> None:
    session = FakeSession([_Resp(500, text="boom")])
    with pytest.raises(SwitchboardApiError):
        await _client(session).send_command({"action_type": "afk_reset_idle"})
    assert session.calls == 1

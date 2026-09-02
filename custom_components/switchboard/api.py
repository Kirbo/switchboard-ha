"""Thin async client for the Switchboard external API (TLS port 38474).

Contract: docs/HA.md in the Switchboard repo. Four read endpoints we use here
(`GET /api/state`, `GET /api/connections`, `GET /api/afk`, `GET /api/events/ws`) sit behind
the Events ACL; `POST /api/command` behind the Control ACL. One bearer token gates all of them.

Writes (`POST /api/command`) share a per-caller budget the app enforces with HTTP 429 — see
`SwitchboardRateLimitError`; it means "slow down", never "broken" or "unauthorised".

TLS is self-signed (the same cert the peer mesh pins), so verification is one of:
- pin the SHA-256 fingerprint (aiohttp.Fingerprint) — recommended,
- skip verification (ssl=False) — simplest,
- full chain verification (ssl=None) — only if the user fronts it with a trusted cert.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp


class SwitchboardApiError(Exception):
    """Any failed call to the Switchboard API."""


class SwitchboardAuthError(SwitchboardApiError):
    """The bearer token was missing/invalid (HTTP 401)."""


class SwitchboardAccessError(SwitchboardApiError):
    """The token is valid but this caller is denied by an ACL (HTTP 403) —
    check the Events/Control ACLs under Settings → External API."""


class SwitchboardRateLimitError(SwitchboardApiError):
    """The caller's write budget is spent (HTTP 429) — the request never ran.

    docs/HA.md "Write budget": every authenticated caller gets 10 writes/second sustained with a
    burst of 30, shared across `POST /api/command`, `POST /api/event` and the equivalent
    websocket frames. It is NOT a broken connection and NOT a rejected credential: the budget
    refills continuously, so the only correct response is to wait and re-send. Retrying
    immediately is what keeps it spent, which is why every retry here sits behind
    `RATE_LIMIT_BACKOFF`.

    A subclass of `SwitchboardApiError` on purpose — every existing caller keeps treating it as
    the transient failure it is, and only the ones that can say something better single it out.
    """


# Waits (seconds) between re-sends of a request the app answered 429 to; one attempt per entry
# plus a final one, so four in total spanning ~1.75 s. The budget refills at 10 writes/second, so
# even the first wait buys a couple of writes back — the contract promises recovery "within a
# second or two", and giving up sooner would fail an automation the app was only asking to slow.
RATE_LIMIT_BACKOFF: tuple[float, ...] = (0.25, 0.5, 1.0)


def build_ssl(verify_ssl: bool, fingerprint: str | None) -> aiohttp.Fingerprint | bool | None:
    """Map the user's TLS choice to an aiohttp `ssl=` value.

    A fingerprint pins the self-signed cert; otherwise verify (None) or skip (False).
    """
    if fingerprint:
        digest = bytes.fromhex(fingerprint.replace(":", "").replace(" ", "").strip())
        return aiohttp.Fingerprint(digest)
    return None if verify_ssl else False


class SwitchboardClient:
    """Issues requests against one Switchboard instance."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str,
        *,
        verify_ssl: bool,
        fingerprint: str | None,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._token = token
        self._ssl = build_ssl(verify_ssl, fingerprint)

    @property
    def base_url(self) -> str:
        return f"https://{self._host}:{self._port}"

    @property
    def ws_url(self) -> str:
        return f"wss://{self._host}:{self._port}/api/events/ws"

    @property
    def ssl(self) -> aiohttp.Fingerprint | bool | None:
        return self._ssl

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    @staticmethod
    async def _retrying(attempt: Callable[[], Awaitable[Any]]) -> Any:
        """Run `attempt`, re-running it after a wait each time the app answers 429.

        The write budget (docs/HA.md) refills continuously, so a throttled request is a "later",
        never a "no". The last attempt's `SwitchboardRateLimitError` propagates so a caller that
        is genuinely over budget still hears about it instead of retrying forever.
        """
        for delay in RATE_LIMIT_BACKOFF:
            try:
                return await attempt()
            except SwitchboardRateLimitError:
                await asyncio.sleep(delay)
        return await attempt()

    async def _get_once(self, path: str) -> Any:
        try:
            async with self._session.get(
                f"{self.base_url}{path}",
                headers=self._headers(),
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    raise SwitchboardAuthError("invalid or missing API token")
                if resp.status == 403:
                    raise SwitchboardAccessError("denied by the External API ACL")
                if resp.status == 429:
                    raise SwitchboardRateLimitError(f"GET {path} -> HTTP 429 (rate limited)")
                if resp.status != 200:
                    raise SwitchboardApiError(f"GET {path} -> HTTP {resp.status}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise SwitchboardApiError(f"GET {path} failed: {err}") from err

    async def _get(self, path: str) -> Any:
        # Reads are not budgeted today (the budget covers writes), but a 429 here would otherwise
        # tear down the events websocket and mark every entity unavailable — a throttle must never
        # read as an outage, so it goes through the same wait-and-re-send path as a write.
        return await self._retrying(lambda: self._get_once(path))

    async def fetch_state(self) -> dict[str, Any]:
        """Current machine snapshot (docs/HA.md `ApiState`): {obs:[...], twitch:[...],
        spotify:'playing|paused|stopped', spotify_now, afk:bool, apps, version, update}."""
        return await self._get("/api/state")

    async def fetch_connections(self) -> list[dict[str, Any]]:
        """Every connection: [{id, integration, label, is_default, enabled, ...}]."""
        return await self._get("/api/connections")

    async def fetch_afk(self) -> dict[str, Any]:
        """Live idle/AFK-countdown numbers (docs/HA.md "AFK numbers"):
        {afk, idle_secs, threshold_secs, snooze_secs, afk_in_secs} — every number nullable.

        A poll endpoint by design (the idle clock resets on every keystroke, so it is never
        pushed as events). We sample it at the few moments the *stable* numbers can change
        rather than running a clock — see the coordinator.
        """
        return await self._get("/api/afk")

    async def send_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/command — run one rule action. Returns {ok, acted}.

        A 429 (write budget spent, docs/HA.md) is retried after a short wait rather than
        surfaced: the app rejected the command BEFORE running it, so re-sending it cannot
        double-act, and an HA script that fires a handful of actions at once must not fail
        because the last two arrived inside the same second.
        """
        return await self._retrying(lambda: self._send_command_once(payload))

    async def _send_command_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._session.post(
                f"{self.base_url}/api/command",
                headers=self._headers(),
                json=payload,
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    raise SwitchboardAuthError("invalid or missing API token")
                if resp.status == 403:
                    raise SwitchboardAccessError("denied by the External API ACL")
                if resp.status == 429:
                    raise SwitchboardRateLimitError("write budget spent — the command was not run")
                if resp.status != 200:
                    text = await resp.text()
                    raise SwitchboardApiError(f"command failed: HTTP {resp.status} {text}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise SwitchboardApiError(f"command failed: {err}") from err

    def ws_connect(self) -> Any:
        """Open the events websocket (caller manages the context + reconnects)."""
        return self._session.ws_connect(
            self.ws_url,
            headers=self._headers(),
            ssl=self._ssl,
            heartbeat=30,
        )

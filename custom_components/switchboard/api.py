"""Thin async client for the Switchboard external API (TLS port 38474).

Contract: docs/HA.md in the Switchboard repo. Three read endpoints we use here
(`GET /api/state`, `GET /api/connections`, `GET /api/events/ws`) sit behind the Events
ACL; `POST /api/command` behind the Control ACL. One bearer token gates all of them.

TLS is self-signed (the same cert the peer mesh pins), so verification is one of:
- pin the SHA-256 fingerprint (aiohttp.Fingerprint) — recommended,
- skip verification (ssl=False) — simplest,
- full chain verification (ssl=None) — only if the user fronts it with a trusted cert.
"""

from __future__ import annotations

from typing import Any

import aiohttp


class SwitchboardApiError(Exception):
    """Any failed call to the Switchboard API."""


class SwitchboardAuthError(SwitchboardApiError):
    """The bearer token was missing/invalid (HTTP 401)."""


class SwitchboardAccessError(SwitchboardApiError):
    """The token is valid but this caller is denied by an ACL (HTTP 403) —
    check the Events/Control ACLs under Settings → External API."""


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

    async def _get(self, path: str) -> Any:
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
                if resp.status != 200:
                    raise SwitchboardApiError(f"GET {path} -> HTTP {resp.status}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise SwitchboardApiError(f"GET {path} failed: {err}") from err

    async def fetch_state(self) -> dict[str, Any]:
        """Current machine snapshot (docs/HA.md `ApiState`): {obs:[...], twitch:[...],
        spotify:'playing|paused|stopped', spotify_now, afk:bool, apps, version, update}."""
        return await self._get("/api/state")

    async def fetch_connections(self) -> list[dict[str, Any]]:
        """Every connection: [{id, integration, label, is_default, enabled, ...}]."""
        return await self._get("/api/connections")

    async def send_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/command — run one rule action. Returns {ok, acted}."""
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

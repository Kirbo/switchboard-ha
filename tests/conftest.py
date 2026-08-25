"""Shared pytest fixtures.

Runs against a real (in-memory) Home Assistant via `pytest-homeassistant-custom-component`,
so the coordinator/entity code is exercised the way HA actually drives it.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load `custom_components/switchboard` in every test."""
    return


class FakeClient:
    """Stands in for `SwitchboardClient`: canned REST payloads, recorded commands."""

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        connections: list[dict[str, Any]] | None = None,
        afk: dict[str, Any] | None = None,
    ) -> None:
        self.state = state if state is not None else {}
        self.connections = connections if connections is not None else []
        self.afk = afk if afk is not None else {}
        self.commands: list[dict[str, Any]] = []
        self.result: dict[str, Any] = {"ok": True, "acted": True}

    async def fetch_state(self) -> dict[str, Any]:
        return self.state

    async def fetch_connections(self) -> list[dict[str, Any]]:
        return self.connections

    async def fetch_afk(self) -> dict[str, Any]:
        return self.afk

    async def send_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.commands.append(payload)
        return self.result


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()

"""Diagnostics for the Switchboard integration — the API token is always redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SwitchboardCoordinator

TO_REDACT = {CONF_TOKEN}
# Which Discord voice channel the streamer sits in, and on which server, says who they are with
# (docs/HA.md "Two read scopes") — a diagnostics file is pasted into public bug reports.
DATA_TO_REDACT = {"channel_name", "guild_name"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: SwitchboardCoordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "connections": coordinator.connections,
        "data": async_redact_data(asdict(coordinator.data), DATA_TO_REDACT),
        "last_update_success": coordinator.last_update_success,
    }

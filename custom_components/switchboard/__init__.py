"""The Switchboard integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import SwitchboardClient
from .const import CONF_FINGERPRINT, DOMAIN
from .coordinator import SwitchboardCoordinator
from .services import async_register_services

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain services once at startup — they survive entry reloads, so a reload of the
    sole entry no longer leaves a window where switchboard.* calls fail."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Switchboard from a config entry."""
    try:
        client = SwitchboardClient(
            async_get_clientsession(hass),
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_TOKEN],
            verify_ssl=entry.data.get(CONF_VERIFY_SSL, False),
            fingerprint=entry.data.get(CONF_FINGERPRINT) or None,
        )
    except ValueError as err:
        # A corrupted stored TLS fingerprint makes bytes.fromhex raise; surface a clean retry
        # instead of an uncaught error so the user can reconfigure the pin.
        raise ConfigEntryNotReady(f"invalid stored TLS fingerprint: {err}") from err
    coordinator = SwitchboardCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # HA doesn't unload a failed setup — don't leave the dead coordinator registered.
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    # Start the ws task only after platform forwarding succeeded — a failed forward would
    # otherwise leak the background task (HA doesn't unload a failed setup).
    await coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: SwitchboardCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unloaded

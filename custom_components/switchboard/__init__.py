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
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
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


def _tls_issue_id(entry: ConfigEntry) -> str:
    return f"insecure_tls_{entry.entry_id}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Switchboard from a config entry."""
    # SB-B-007. The SB-A-061 guard was added to the config FLOW, so it only ever protected NEW
    # configurations. This function rebuilds the client straight from `entry.data`, and every
    # install created before that fix — when `verify_ssl: False` with an empty fingerprint was the
    # shipped default — kept handing the GLOBAL Switchboard API token to anything able to ARP-spoof
    # the segment, on every poll, indefinitely. There was no migration and no repair; the
    # integration simply carried on.
    #
    # Refusing to load is the same answer the config flow already gives, and it is the only one that
    # actually stops the leak: continuing to poll IS the vulnerability. `ConfigEntryError` (not
    # `ConfigEntryNotReady`) so HA does not retry in a loop, paired with a repairs issue that says
    # what to do — a silent failure would be worse than the insecure connection it replaces.
    if (
        not entry.data.get(CONF_VERIFY_SSL, False)
        and not (entry.data.get(CONF_FINGERPRINT) or "").strip()
    ):
        ir.async_create_issue(
            hass,
            DOMAIN,
            _tls_issue_id(entry),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="insecure_tls",
            translation_placeholders={"host": str(entry.data.get(CONF_HOST, ""))},
        )
        raise ConfigEntryError(
            "This Switchboard connection cannot be authenticated: certificate verification is off "
            "and no TLS fingerprint is pinned, so the API token would be readable by anything on "
            "the network. Reconfigure the entry and set the fingerprint from Switchboard's Peers "
            "tab."
        )
    # Authenticatable now — retire any issue raised by an earlier load.
    ir.async_delete_issue(hass, DOMAIN, _tls_issue_id(entry))
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

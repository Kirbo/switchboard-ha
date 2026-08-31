"""Setup-time guards in `__init__.py`."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.switchboard.const import CONF_FINGERPRINT, DOMAIN

from .conftest import FakeClient
from .test_contract import API_AFK, API_STATE
from .test_coordinator import CONNECTIONS

INSECURE = {
    CONF_HOST: "192.0.2.10",
    CONF_PORT: 38474,
    CONF_TOKEN: "test-token",
    CONF_VERIFY_SSL: False,
}
PINNED = {**INSECURE, CONF_FINGERPRINT: "ab" * 32}


async def _try_setup(hass: HomeAssistant, data: dict) -> tuple[MockConfigEntry, bool]:
    entry = MockConfigEntry(domain=DOMAIN, data=data, title="Switchboard (test)")
    entry.add_to_hass(hass)
    client = FakeClient(state=API_STATE, connections=list(CONNECTIONS), afk=API_AFK)
    with (
        patch("custom_components.switchboard.SwitchboardClient", return_value=client),
        patch(
            "custom_components.switchboard.coordinator.SwitchboardCoordinator.async_start",
            return_value=None,
        ),
    ):
        ok = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, ok


async def test_an_entry_with_no_tls_trust_refuses_to_load(hass: HomeAssistant) -> None:
    """SB-B-007: the SB-A-061 guard was added to the config FLOW only.

    `async_setup_entry` rebuilt the client straight from `entry.data`, so every install created
    before that fix — when `verify_ssl: False` with an empty fingerprint was the shipped default —
    kept handing the GLOBAL Switchboard API token to anything that could ARP-spoof the segment,
    on every poll, for as long as it kept running. There was no migration and no repairs issue:
    the integration simply carried on.
    """
    entry, ok = await _try_setup(hass, INSECURE)
    assert not ok, "an unauthenticatable TLS setup must not load"

    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"insecure_tls_{entry.entry_id}") is not None, (
        "the user needs a repairs issue telling them how to fix it — a silent failure is worse "
        "than the insecure connection it replaced"
    )


async def test_a_pinned_entry_loads(hass: HomeAssistant) -> None:
    """The guard must not fire for a configuration that IS authenticatable."""
    _entry, ok = await _try_setup(hass, PINNED)
    assert ok, "a fingerprint-pinned entry is authenticatable and must load"


async def test_fixing_the_entry_clears_the_issue(hass: HomeAssistant) -> None:
    """The real upgrade path: the entry fails, the user reconfigures, the repair goes away.

    Asserting "no issue" on an entry that never raised one would pass with the clearing code
    deleted — the issue has to exist first for its removal to mean anything.
    """
    entry, ok = await _try_setup(hass, INSECURE)
    assert not ok
    issue_id = f"insecure_tls_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # The user pastes the fingerprint via Reconfigure, which updates the entry and reloads it.
    hass.config_entries.async_update_entry(entry, data=PINNED)
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

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None, (
        "the repairs issue outlived the problem — the user fixed it and is still being told to"
    )


async def test_verified_tls_also_loads(hass: HomeAssistant) -> None:
    """Verification against a real CA is the other authenticatable setup."""
    _entry, ok = await _try_setup(hass, {**INSECURE, CONF_VERIFY_SSL: True})
    assert ok

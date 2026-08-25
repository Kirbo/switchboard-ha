"""End-to-end entity setup: the config entry loads and every entity reads the snapshot."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.switchboard.const import DOMAIN

from .conftest import FakeClient
from .test_contract import API_AFK, API_STATE
from .test_coordinator import CONNECTIONS

ENTRY_DATA = {
    CONF_HOST: "192.0.2.10",
    CONF_PORT: 38474,
    CONF_TOKEN: "test-token",
    CONF_VERIFY_SSL: False,
}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, title="Switchboard (test)")
    entry.add_to_hass(hass)
    client = FakeClient(state=API_STATE, connections=list(CONNECTIONS), afk=API_AFK)
    with (
        patch("custom_components.switchboard.SwitchboardClient", return_value=client),
        # The events websocket is exercised in the coordinator tests; keep it out of setup.
        patch(
            "custom_components.switchboard.coordinator.SwitchboardCoordinator.async_start",
            return_value=None,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_entities_are_created_from_the_snapshot(hass: HomeAssistant) -> None:
    await _setup(hass)

    obs = "home_obs"
    assert hass.states.get(f"sensor.{obs}_scene").state == "Gaming"
    assert hass.states.get(f"binary_sensor.{obs}_connected").state == "on"
    assert hass.states.get(f"binary_sensor.{obs}_streaming").state == "on"
    assert hass.states.get(f"binary_sensor.{obs}_recording").state == "off"
    assert hass.states.get(f"sensor.{obs}_stream_delay").state == "20"
    assert hass.states.get(f"sensor.{obs}_stream_started").state == "2023-11-14T22:13:20+00:00"

    assert hass.states.get("sensor.main_viewers").state == "42"
    assert hass.states.get("sensor.main_chatters").state == "7"
    assert hass.states.get("sensor.main_category").state == "Just Chatting"
    assert hass.states.get("binary_sensor.main_live").state == "on"
    assert hass.states.get("sensor.main_live_since").state == "2023-11-14T22:13:20+00:00"

    hub = "switchboard_test"
    spotify = hass.states.get(f"sensor.{hub}_spotify")
    assert spotify.state == "playing"
    assert spotify.attributes["title"] == "Song"
    assert spotify.attributes["up_next_title"] == "Next"
    assert "position_ms" not in spotify.attributes

    afk = hass.states.get(f"binary_sensor.{hub}_afk")
    assert afk.state == "off"
    assert afk.attributes["threshold_secs"] == 180
    assert afk.attributes["snooze_until"] is None

    focused = hass.states.get(f"sensor.{hub}_focused_app")
    assert focused.state == "steam_app_599140"
    assert focused.attributes["watched_focused"] is True
    assert focused.attributes["watched_running"] is False
    assert focused.attributes["running"] == ["steam_app_599140"]

    assert hass.states.get(f"binary_sensor.{hub}_watched_app_active").state == "on"
    assert hass.states.get(f"sensor.{hub}_version").state == "2026.6.10"
    update = hass.states.get(f"binary_sensor.{hub}_update_available")
    assert update.state == "on"
    assert update.attributes["version"] == "2026.7.1"


async def test_services_are_registered(hass: HomeAssistant) -> None:
    await _setup(hass)
    for service in (
        "run_action",
        "obs_scene_set",
        "overlay_alert",
        "set_machine_state",
        "light_flash",
        "afk_snooze",
        "afk_reset_idle",
    ):
        assert hass.services.has_service(DOMAIN, service), service


async def test_diagnostics_redact_the_token(hass: HomeAssistant) -> None:
    from custom_components.switchboard.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = await _setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"][CONF_TOKEN] == "**REDACTED**"
    assert diag["data"]["obs"]
    assert diag["connections"]


async def test_unload(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data.get(DOMAIN, {})

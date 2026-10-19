"""End-to-end entity setup: the config entry loads and every entity reads the snapshot."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.switchboard.const import CONF_FINGERPRINT, DOMAIN

from .conftest import FakeClient
from .test_contract import ALERT_PRESET, API_AFK, API_STATE
from .test_coordinator import CONNECTIONS

# A pinned fingerprint, because SB-B-007 makes `verify_ssl: False` with no fingerprint refuse to
# load — that pair was the old shipped default and is exactly what the guard exists to stop.
ENTRY_DATA = {
    CONF_HOST: "192.0.2.10",
    CONF_PORT: 38474,
    CONF_TOKEN: "test-token",
    CONF_VERIFY_SSL: False,
    CONF_FINGERPRINT: "ab" * 32,
}


async def _setup(
    hass: HomeAssistant, state: dict | None = None, options: dict | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=options or {}, title="Switchboard (test)"
    )
    entry.add_to_hass(hass)
    client = FakeClient(
        state=API_STATE if state is None else state, connections=list(CONNECTIONS), afk=API_AFK
    )
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
    assert hass.states.get("sensor.main_followers").state == "1234"
    assert hass.states.get("sensor.main_subscribers").state == "56"
    assert hass.states.get("sensor.main_category").state == "Just Chatting"
    assert hass.states.get("binary_sensor.main_live").state == "on"
    assert hass.states.get("sensor.main_live_since").state == "2023-11-14T22:13:20+00:00"

    hub = "switchboard_test"
    spotify = hass.states.get(f"sensor.{hub}_spotify")
    assert spotify.state == "playing"
    assert spotify.attributes["title"] == "Song"
    assert spotify.attributes["up_next_title"] == "Next"
    assert "position_ms" not in spotify.attributes

    overlays = hass.states.get(f"binary_sensor.{hub}_input_overlays_visible")
    assert overlays.state == "off"
    assert overlays.attributes["auto_show_at"] == "2023-11-14T22:18:20+00:00"

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


async def test_needs_reauth_sensor_is_off_until_a_credential_is_rejected(
    hass: HomeAssistant,
) -> None:
    """One hub-level PROBLEM sensor covers every connection carrying `needs_reauth`
    (docs/HA.md) — Twitch accounts and the app's downstream Home Assistant connections alike."""
    entry = await _setup(hass)
    coord = hass.data[DOMAIN][entry.entry_id]
    sensor = "binary_sensor.switchboard_test_connection_needs_re_authentication"
    assert hass.states.get(sensor).state == "off"

    coord.connections = [
        {**c, "needs_reauth": c["integration"] == "twitch"} for c in coord.connections
    ]
    coord.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(sensor)
    assert state.state == "on"
    assert state.attributes["connections"] == ["Main"]
    assert state.attributes["integrations"] == ["twitch"]


PAUSED = "binary_sensor.switchboard_test_main_alerts_paused"
QUEUED = "sensor.switchboard_test_main_alerts_queued"


async def test_alert_preset_entities_mirror_the_alerts_slice(hass: HomeAssistant) -> None:
    """docs/HA.md "Alert presets": one `paused` binary sensor + one `queued` sensor per preset."""
    entry = await _setup(hass)
    assert hass.states.get(PAUSED).state == "off"
    queued = hass.states.get(QUEUED)
    assert queued.state == "3"
    assert queued.attributes["showing"] == "twitch_raid"
    assert queued.attributes["dropped"] == 0
    assert queued.attributes["merged"] == 14
    assert queued.attributes["preset_id"] == ALERT_PRESET

    coord = hass.data[DOMAIN][entry.entry_id]
    coord._handle_frame(
        {
            "type": "alert_queue_changed",
            "preset_id": ALERT_PRESET,
            "name": "Main alerts",
            "paused": True,
            "queued": 5,
            "dropped": 1,
            "merged": 14,
            "showing": None,
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get(PAUSED).state == "on"
    queued = hass.states.get(QUEUED)
    assert queued.state == "5"
    assert queued.attributes["showing"] is None
    assert queued.attributes["dropped"] == 1


async def test_alert_preset_entities_come_and_go_with_the_presets(hass: HomeAssistant) -> None:
    """A preset created in the app gets its entities on its first frame; the deleted-preset frame
    removes them from the registry — no reload, no lingering `unavailable` rows."""
    entry = await _setup(hass)
    coord = hass.data[DOMAIN][entry.entry_id]
    other = "55555555-5555-5555-5555-555555555555"
    coord._handle_frame(
        {
            "type": "alert_queue_changed",
            "preset_id": other,
            "name": "Second",
            "paused": False,
            "queued": 1,
            "dropped": 0,
            "merged": 0,
            "showing": "twitch_follow",
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.switchboard_test_second_queued").state == "1"
    assert hass.states.get("binary_sensor.switchboard_test_second_paused").state == "off"

    coord._handle_frame(
        {
            "type": "alert_queue_changed",
            "preset_id": ALERT_PRESET,
            "name": "",
            "paused": False,
            "queued": 0,
            "dropped": 0,
            "merged": 0,
            "showing": None,
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get(PAUSED) is None
    assert hass.states.get(QUEUED) is None
    registry = er.async_get(hass)
    assert registry.async_get(QUEUED) is None
    assert registry.async_get(PAUSED) is None
    # The other preset is untouched.
    assert hass.states.get("sensor.switchboard_test_second_queued").state == "1"


async def test_an_app_without_alert_presets_gets_no_alert_entities(hass: HomeAssistant) -> None:
    """`alerts` is additive (2026-10-15): an older app omits it — no entities, no errors."""
    await _setup(hass, {k: v for k, v in API_STATE.items() if k != "alerts"})
    assert hass.states.get(PAUSED) is None
    assert hass.states.get(QUEUED) is None
    assert hass.states.get("sensor.home_obs_scene").state == "Gaming"


async def test_a_preset_deleted_while_ha_was_down_is_purged_at_setup(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert hass.states.get(QUEUED) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    client = FakeClient(
        state={**API_STATE, "alerts": {}}, connections=list(CONNECTIONS), afk=API_AFK
    )
    with (
        patch("custom_components.switchboard.SwitchboardClient", return_value=client),
        patch(
            "custom_components.switchboard.coordinator.SwitchboardCoordinator.async_start",
            return_value=None,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get(QUEUED) is None
    assert registry.async_get(PAUSED) is None


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
        "alert_skip",
        "alert_pause",
        "alert_resume",
        "alert_replay",
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


STATUS = "sensor.switchboard_test_connection_status"


async def test_connection_status_sensor_rolls_the_rows_up(hass: HomeAssistant) -> None:
    """docs/HA.md "Connection status": the worst state, the problem count, each integration's
    worst state and every row; live from the push events."""
    entry = await _setup(hass)
    state = hass.states.get(STATUS)
    assert state.state == "connected"
    assert state.attributes["problems"] == 0
    assert state.attributes["by_integration"] == {"twitch": "connected"}

    coord = hass.data[DOMAIN][entry.entry_id]
    coord._handle_frame(
        {
            "type": "connection_status_changed",
            "entry": {
                "key": "obs/connection/o1",
                "integration": "obs",
                "component": "connection",
                "connection_id": "o1",
                "subject": None,
                "label": None,
                "state": "reconnecting",
                "since_ms": 1760800000000,
                "last_error": "connect timed out",
                "last_error_ms": 1760800000000,
                "drops": 1,
                "reconnects": 0,
                "crashes": 0,
                "queued": None,
            },
        }
    )
    await hass.async_block_till_done()
    state = hass.states.get(STATUS)
    assert state.state == "reconnecting"
    assert state.attributes["problems"] == 1
    assert state.attributes["by_integration"]["obs"] == "reconnecting"
    assert [r["key"] for r in state.attributes["rows"]][0] == "obs/connection/o1"
    assert state.attributes["rows"][0]["last_error"] == "connect timed out"


async def test_an_uncounted_row_is_listed_but_never_raises_the_sensor(hass: HomeAssistant) -> None:
    """A row the app marks `counted: false` (a peer whose "Counts toward Ready to stream and
    attention" is off, app 2026-10-19+): in `rows`, never in the state, `problems` or
    `by_integration`; counted again, it is a problem like any other."""
    entry = await _setup(hass)
    coord = hass.data[DOMAIN][entry.entry_id]

    def vm(counted: bool) -> dict:
        return {
            "type": "connection_status_changed",
            "entry": {
                "key": "peer/link/vm",
                "integration": "peer",
                "component": "link",
                "connection_id": None,
                "subject": "vm",
                "label": "SB-WIN11",
                "state": "down",
                "since_ms": 1760800000000,
                "last_error": "unreachable — connection refused",
                "last_error_ms": 1760800000000,
                "drops": 0,
                "reconnects": 0,
                "crashes": 0,
                "queued": None,
                "counted": counted,
            },
        }

    coord._handle_frame(vm(False))
    await hass.async_block_till_done()
    state = hass.states.get(STATUS)
    assert state.state == "connected"
    assert state.attributes["problems"] == 0
    assert state.attributes["by_integration"]["peer"] == "disabled"
    vm_row = next(r for r in state.attributes["rows"] if r["key"] == "peer/link/vm")
    assert vm_row["counted"] is False
    assert vm_row["state"] == "down"

    coord._handle_frame(vm(True))
    await hass.async_block_till_done()
    state = hass.states.get(STATUS)
    assert state.state == "down"
    assert state.attributes["problems"] == 1
    assert state.attributes["by_integration"]["peer"] == "down"

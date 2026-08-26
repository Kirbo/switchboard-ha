"""Services → the exact `POST /api/command` payloads docs/HA.md documents."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
import voluptuous as vol

from custom_components.switchboard.const import DOMAIN

from .test_entities import _setup

HA_CONN = "33333333-3333-3333-3333-333333333333"
OBS_CONN = "11111111-1111-1111-1111-111111111111"
TWITCH_CONN = "22222222-2222-2222-2222-222222222222"


def _client(hass: HomeAssistant, entry_id: str):
    return hass.data[DOMAIN][entry_id].client


async def _call(hass: HomeAssistant, service: str, data: dict) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)


async def test_obs_scene_set_resolves_a_label(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _call(hass, "obs_scene_set", {"target": "Home OBS", "scene": "BRB"})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "obs_scene_set",
        "target_connection_id": OBS_CONN,
        "value": "BRB",
    }


async def test_go_live_resolves_labels_for_account_and_obs(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _call(hass, "go_live", {"account_id": "Main", "obs_id": "Home OBS"})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "twitch_go_live",
        "target_connection_id": TWITCH_CONN,
        "value": "",
        "action_params": {"obs_id": OBS_CONN},
    }


async def test_go_live_omits_action_params_so_the_app_default_obs_applies(
    hass: HomeAssistant,
) -> None:
    """No `obs_id` → no `action_params` at all: both target keys absent means the app resolves
    its default local OBS (docs/HA.md Commands, `twitch_go_live` row)."""
    entry = await _setup(hass)
    await _call(hass, "go_live", {"account_id": TWITCH_CONN})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "twitch_go_live",
        "target_connection_id": TWITCH_CONN,
        "value": "",
    }


async def test_run_action_passes_through_unknown_targets(hass: HomeAssistant) -> None:
    """Action sentinels like `spotify` aren't connections — they must reach the app untouched."""
    entry = await _setup(hass)
    await _call(
        hass,
        "run_action",
        {
            "action_type": "spotify_playback_start",
            "target": "spotify",
            "value": "spotify:track:x",
            "action_params": {"remember": True},
        },
    )
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "spotify_playback_start",
        "target_connection_id": "spotify",
        "value": "spotify:track:x",
        "action_params": {"remember": True},
    }


async def test_overlay_alert_and_machine_state(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _call(hass, "overlay_alert", {"text": "BRB"})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "overlay_alert_show",
        "value": "BRB",
    }
    await _call(hass, "set_machine_state", {"state": "afk"})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "machine_state_set",
        "value": "afk",
    }


async def test_light_flash_builds_the_ha_flash_params(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _call(
        hass,
        "light_flash",
        {
            "entity_id": "light.nayttovalot",
            "flashes": 5,
            "color": "#a855f7",
            "brightness": 200,
            "on_ms": 400,
            "off_ms": 400,
            "transition_ms": 100,
        },
    )
    # Target omitted → the app's default HA connection, resolved from /api/connections.
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "ha_light_flash",
        "target_connection_id": HA_CONN,
        "value": "",
        "action_params": {
            "ha_flash": {
                "entity_id": "light.nayttovalot",
                "flashes": 5,
                "color": "#a855f7",
                "brightness": 200,
                "on_ms": 400,
                "off_ms": 400,
                "transition_ms": 100,
            }
        },
    }


async def test_light_flash_omits_unset_params_so_the_app_defaults_apply(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass)
    await _call(hass, "light_flash", {"entity_id": "light.a", "target": "Studio HA"})
    assert _client(hass, entry.entry_id).commands[-1]["action_params"] == {
        "ha_flash": {"entity_id": "light.a"}
    }


async def test_light_flash_rejects_out_of_range_values(hass: HomeAssistant) -> None:
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await _call(hass, "light_flash", {"entity_id": "light.a", "flashes": 99})
    with pytest.raises(vol.Invalid):
        await _call(hass, "light_flash", {"entity_id": "light.a", "color_temp_kelvin": 100})


async def test_afk_services(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _call(hass, "afk_snooze", {"seconds": 900})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "afk_snooze",
        "value": "900",
    }
    await _call(hass, "afk_reset_idle", {})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "afk_reset_idle",
        "value": "",
    }


async def test_a_rejected_command_raises(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    _client(hass, entry.entry_id).result = {"ok": False, "error": "nope"}
    with pytest.raises(HomeAssistantError):
        await _call(hass, "overlay_alert", {"text": "x"})


async def test_unknown_entry_id_raises(hass: HomeAssistant) -> None:
    await _setup(hass)
    with pytest.raises(HomeAssistantError, match="no Switchboard config entry"):
        await _call(hass, "overlay_alert", {"text": "x", "entry_id": "bogus"})

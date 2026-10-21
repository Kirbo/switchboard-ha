"""Services → the exact `POST /api/command` payloads docs/HA.md documents."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
import voluptuous as vol

from custom_components.switchboard.api import (
    SwitchboardAccessError,
    SwitchboardApiError,
    SwitchboardRateLimitError,
)
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


async def test_apply_scene_stream_info_targets_the_obs_connection(hass: HomeAssistant) -> None:
    """Re-apply the Stream Info mapped to the OBS connection's current scene: the app reads the
    scene itself, so the command carries only the (label-resolved) OBS target."""
    entry = await _setup(hass)
    await _call(hass, "apply_scene_stream_info", {"target": "Home OBS"})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "twitch_stream_info_apply",
        "target_connection_id": OBS_CONN,
        "value": "",
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


async def test_run_action_forwards_target_peer_id_and_keeps_the_peer_target_verbatim(
    hass: HomeAssistant,
) -> None:
    """docs/HA.md Commands: `target_peer_id` routes the action to that paired peer, and the
    target then names a connection ON THE PEER — so a label that happens to match a LOCAL
    connection must not be swapped for the local id."""
    entry = await _setup(hass)
    await _call(
        hass,
        "run_action",
        {
            "action_type": "obs_scene_set",
            "target": "Home OBS",
            "value": "BRB",
            "target_peer_id": "peer-1",
        },
    )
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "obs_scene_set",
        "target_connection_id": "Home OBS",
        "value": "BRB",
        "action_params": {},
        "target_peer_id": "peer-1",
    }


async def test_run_action_without_a_peer_sends_no_target_peer_id(hass: HomeAssistant) -> None:
    """Blank/absent = run locally, exactly as before: the key is not sent and the target still
    resolves against this instance's labels."""
    entry = await _setup(hass)
    await _call(
        hass,
        "run_action",
        {"action_type": "obs_scene_set", "target": "Home OBS", "value": "BRB"},
    )
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "obs_scene_set",
        "target_connection_id": OBS_CONN,
        "value": "BRB",
        "action_params": {},
    }


async def test_run_action_sends_the_auto_account_verbatim(hass: HomeAssistant) -> None:
    """docs/HA.md Commands: `target_connection_id: "auto"` = the Auto Twitch account (the live
    one). It is a sentinel, never a label — even a connection that happens to be labelled "auto"
    must not be swapped in for it."""
    entry = await _setup(hass)
    hass.data[DOMAIN][entry.entry_id].connections.append(
        {"id": "44444444-4444-4444-4444-444444444444", "integration": "twitch", "label": "auto"}
    )
    await _call(
        hass,
        "run_action",
        {"action_type": "twitch_clip_create", "target": "auto", "multi_live": "all"},
    )
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "twitch_clip_create",
        "target_connection_id": "auto",
        "value": "",
        "action_params": {"multi_live": "all"},
    }


async def test_run_action_omits_multi_live_when_it_is_the_default(hass: HomeAssistant) -> None:
    """Absent `multi_live` means `default` on the app side — sending it adds nothing."""
    entry = await _setup(hass)
    await _call(
        hass,
        "run_action",
        {"action_type": "twitch_marker_create", "target": "auto", "multi_live": "default"},
    )
    assert _client(hass, entry.entry_id).commands[-1]["action_params"] == {}


async def test_run_action_rejects_an_unknown_multi_live(hass: HomeAssistant) -> None:
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await _call(
            hass,
            "run_action",
            {"action_type": "twitch_clip_create", "target": "auto", "multi_live": "first"},
        )


async def test_run_action_posts_to_a_discord_route(hass: HomeAssistant) -> None:
    """A route is a blank target + `action_params.discord_route` (the NAME). `twitch_account`
    resolves a local label to its id; Auto is the default and is omitted."""
    entry = await _setup(hass)
    await _call(
        hass,
        "run_action",
        {
            "action_type": "discord_webhook_send",
            "value": "New clip!",
            "discord_route": "Clips",
            "twitch_account": "Main",
            "action_params": {"keep": True},
        },
    )
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "discord_webhook_send",
        "target_connection_id": "",
        "value": "New clip!",
        "action_params": {"keep": True, "discord_route": "Clips", "twitch_account": TWITCH_CONN},
    }
    await _call(
        hass,
        "run_action",
        {
            "action_type": "discord_webhook_send",
            "value": "x",
            "discord_route": "Clips",
            "twitch_account": "auto",
            "multi_live": "skip",
        },
    )
    assert _client(hass, entry.entry_id).commands[-1]["action_params"] == {
        "discord_route": "Clips",
        "multi_live": "skip",
    }


async def test_run_action_passes_route_params_through_action_params(hass: HomeAssistant) -> None:
    """The raw contract keys in `action_params` reach the app untouched (forward-compatible)."""
    entry = await _setup(hass)
    params = {"discord_route": "Moderation", "twitch_account": "auto", "multi_live": "all"}
    await _call(
        hass,
        "run_action",
        {"action_type": "discord_webhook_send", "value": "x", "action_params": params},
    )
    assert _client(hass, entry.entry_id).commands[-1]["action_params"] == params


async def test_run_action_keeps_a_peer_twitch_account_verbatim(hass: HomeAssistant) -> None:
    """On a peer the account is a connection ON THE PEER: never resolved against local labels."""
    entry = await _setup(hass)
    await _call(
        hass,
        "run_action",
        {
            "action_type": "discord_webhook_send",
            "value": "x",
            "discord_route": "Clips",
            "twitch_account": "Main",
            "target_peer_id": "peer-1",
        },
    )
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "discord_webhook_send",
        "target_connection_id": "",
        "value": "x",
        "action_params": {"discord_route": "Clips", "twitch_account": "Main"},
        "target_peer_id": "peer-1",
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


async def test_set_input_overlay_visible_maps_to_the_three_actions(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    for visible, action in (
        ("false", "input_overlay_hide"),
        ("true", "input_overlay_show"),
        ("toggle", "input_overlay_toggle"),
    ):
        await _call(hass, "set_input_overlay_visible", {"visible": visible})
        assert _client(hass, entry.entry_id).commands[-1] == {"action_type": action, "value": ""}
    # The default is a toggle — one automation button, no state to track.
    await _call(hass, "set_input_overlay_visible", {})
    assert _client(hass, entry.entry_id).commands[-1]["action_type"] == "input_overlay_toggle"
    with pytest.raises(vol.Invalid):
        await _call(hass, "set_input_overlay_visible", {"visible": "maybe"})


async def test_a_rejected_command_raises(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    _client(hass, entry.entry_id).result = {"ok": False, "error": "nope"}
    with pytest.raises(HomeAssistantError):
        await _call(hass, "overlay_alert", {"text": "x"})


async def test_a_throttled_command_says_it_did_not_run(hass: HomeAssistant) -> None:
    """The client already backed off and re-sent (docs/HA.md write budget); if it is STILL
    throttled the service must say the action did not run and why, rather than the opaque
    "command failed: HTTP 429" that reads like the app broke."""
    entry = await _setup(hass)
    _client(hass, entry.entry_id).error = SwitchboardRateLimitError("write budget spent")
    with pytest.raises(HomeAssistantError, match="rate-limiting"):
        await _call(hass, "overlay_alert", {"text": "x"})


async def test_unknown_entry_id_raises(hass: HomeAssistant) -> None:
    await _setup(hass)
    with pytest.raises(HomeAssistantError, match="no Switchboard config entry"):
        await _call(hass, "overlay_alert", {"text": "x", "entry_id": "bogus"})


@pytest.mark.parametrize(
    ("service", "action_type"),
    [
        ("alert_skip", "overlay_alert_skip"),
        ("alert_pause", "overlay_alert_pause"),
        ("alert_resume", "overlay_alert_resume"),
        ("alert_replay", "overlay_alert_replay"),
    ],
)
async def test_alert_controls_name_the_preset_in_action_params(
    hass: HomeAssistant, service: str, action_type: str
) -> None:
    """docs/HA.md Commands: the preset (id or case-insensitive name — the app resolves it) rides
    `action_params.alert_preset`; blank = every preset, so nothing is sent for it."""
    entry = await _setup(hass)
    await _call(hass, service, {"preset": "Main alerts"})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": action_type,
        "value": "",
        "action_params": {"alert_preset": "Main alerts"},
    }
    await _call(hass, service, {})
    assert _client(hass, entry.entry_id).commands[-1] == {"action_type": action_type, "value": ""}


async def test_an_alert_control_the_app_refuses_says_why(hass: HomeAssistant) -> None:
    """An app older than the alert presets 400s the unknown action type; a current one 400s an
    unknown preset. Either way the error names the likely causes, not just "HTTP 400"."""
    entry = await _setup(hass)
    _client(hass, entry.entry_id).error = SwitchboardApiError(
        "command failed: HTTP 400 unknown action_type"
    )
    with pytest.raises(HomeAssistantError, match="2026-10-15"):
        await _call(hass, "alert_pause", {"preset": "Nope"})


@pytest.mark.parametrize(
    ("action", "action_type"),
    [
        ("mute", "discord_voice_mute"),
        ("unmute", "discord_voice_unmute"),
        ("toggle_mute", "discord_voice_mute_toggle"),
        ("deafen", "discord_voice_deafen"),
        ("undeafen", "discord_voice_undeafen"),
        ("toggle_deafen", "discord_voice_deafen_toggle"),
        ("toggle_mode", "discord_voice_mode_toggle"),
    ],
)
async def test_discord_voice_maps_each_action_with_a_blank_target(
    hass: HomeAssistant, action: str, action_type: str
) -> None:
    """docs/HA.md Commands: `discord_voice_*` act on the machine's own Discord client — the target
    is sent blank (a blank target never resolves to a Discord webhook) and the value empty."""
    entry = await _setup(hass)
    await _call(hass, "discord_voice", {"action": action})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": action_type,
        "target_connection_id": "",
        "value": "",
    }


async def test_discord_voice_set_mode_sends_the_mode_as_the_value(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _call(hass, "discord_voice", {"action": "set_mode", "mode": "push_to_talk"})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "discord_voice_mode_set",
        "target_connection_id": "",
        "value": "push_to_talk",
    }


async def test_discord_voice_runs_on_a_peer_machine(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _call(hass, "discord_voice", {"action": "deafen", "target_peer_id": " peer-1 "})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "discord_voice_deafen",
        "target_connection_id": "",
        "value": "",
        "target_peer_id": "peer-1",
    }


async def test_discord_voice_validates_the_mode(hass: HomeAssistant) -> None:
    """`mode` is required by set_mode, refused on every other action, and only the two modes the
    contract names are accepted — nothing reaches the app for any of these."""
    entry = await _setup(hass)
    with pytest.raises(vol.Invalid, match="required"):
        await _call(hass, "discord_voice", {"action": "set_mode"})
    with pytest.raises(vol.Invalid, match="only applies"):
        await _call(hass, "discord_voice", {"action": "toggle_mute", "mode": "push_to_talk"})
    with pytest.raises(vol.Invalid):
        await _call(hass, "discord_voice", {"action": "set_mode", "mode": "always_on"})
    with pytest.raises(vol.Invalid):
        await _call(hass, "discord_voice", {"action": "shout"})
    assert _client(hass, entry.entry_id).commands == []


async def test_discord_voice_says_why_the_app_refused(hass: HomeAssistant) -> None:
    """A 400 (Discord not running / not signed in, or an app older than 2026-10-21) and a 403 (a
    plugin token without the `discord_voice` scope) each name what to check."""
    entry = await _setup(hass)
    client = _client(hass, entry.entry_id)
    client.error = SwitchboardApiError("command failed: HTTP 400 discord is not running")
    with pytest.raises(HomeAssistantError, match="Discord desktop app running"):
        await _call(hass, "discord_voice", {"action": "mute"})
    client.error = SwitchboardAccessError("denied by the External API ACL")
    with pytest.raises(HomeAssistantError, match="discord_voice` scope"):
        await _call(hass, "discord_voice", {"action": "mute"})
    # Anything else keeps the plain error.
    client.error = SwitchboardApiError("command failed: HTTP 500 boom")
    with pytest.raises(HomeAssistantError, match="command failed: HTTP 500") as err:
        await _call(hass, "discord_voice", {"action": "mute"})
    assert "Discord desktop app" not in str(err.value)


async def test_discord_voice_rejected_result_raises(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    _client(hass, entry.entry_id).result = {"ok": False}
    with pytest.raises(HomeAssistantError, match="rejected"):
        await _call(hass, "discord_voice", {"action": "toggle_deafen"})


@pytest.mark.parametrize(
    ("data", "action_type", "value"),
    [
        ({"mode": "set", "amount": -6}, "obs_audio_volume_set", "-6dB"),
        ({"amount": -6.5, "unit": "db"}, "obs_audio_volume_set", "-6.5dB"),
        ({"mode": "set", "amount": 50, "unit": "percent"}, "obs_audio_volume_set", "50%"),
        ({"mode": "up", "amount": 3}, "obs_audio_volume_up", "3dB"),
        ({"mode": "down", "amount": 5, "unit": "percent"}, "obs_audio_volume_down", "5%"),
        # No step = the app's own default (3 dB).
        ({"mode": "up"}, "obs_audio_volume_up", ""),
    ],
)
async def test_set_volume_on_an_obs_input(
    hass: HomeAssistant, data: dict, action_type: str, value: str
) -> None:
    """docs/HA.md Commands: the fader level/step in dB (suffixed) or % of OBS's slider; the input
    name rides `action_params.input`; the OBS connection resolves label → id."""
    entry = await _setup(hass)
    await _call(
        hass,
        "set_volume",
        {"target": "obs_input", "input": "Mic/Aux", "connection": "Home OBS", **data},
    )
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": action_type,
        "target_connection_id": OBS_CONN,
        "value": value,
        "action_params": {"input": "Mic/Aux"},
    }


async def test_set_volume_blank_obs_connection_is_the_app_default(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _call(hass, "set_volume", {"target": "obs_input", "input": "Mic/Aux", "amount": 0})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "obs_audio_volume_set",
        "target_connection_id": "",
        "value": "0dB",
        "action_params": {"input": "Mic/Aux"},
    }


async def test_set_volume_on_a_peer_keeps_its_obs_connection_verbatim(
    hass: HomeAssistant,
) -> None:
    """A peer's OBS is named by the PEER's id — a label matching a local OBS must not be swapped."""
    entry = await _setup(hass)
    await _call(
        hass,
        "set_volume",
        {
            "target": "obs_input",
            "input": "Mic/Aux",
            "connection": "Home OBS",
            "mode": "down",
            "target_peer_id": "peer-1",
        },
    )
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "obs_audio_volume_down",
        "target_connection_id": "Home OBS",
        "value": "",
        "action_params": {"input": "Mic/Aux"},
        "target_peer_id": "peer-1",
    }


@pytest.mark.parametrize(
    ("data", "action_type", "value"),
    [
        ({"amount": 50}, "machine_audio_volume_set", "50"),
        ({"amount": 50, "unit": "percent"}, "machine_audio_volume_set", "50"),
        ({"mode": "up", "amount": 10}, "machine_audio_volume_up", "10"),
        # No step = the app's own default (5 %).
        ({"mode": "down"}, "machine_audio_volume_down", ""),
    ],
)
async def test_set_volume_on_the_system_output(
    hass: HomeAssistant, data: dict, action_type: str, value: str
) -> None:
    """The OS output: a bare percent, no connection (blank target), the default output unless a
    device id is given."""
    entry = await _setup(hass)
    await _call(hass, "set_volume", {"target": "system_output", **data})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": action_type,
        "target_connection_id": "",
        "value": value,
    }


async def test_set_volume_names_the_output_device_and_peer(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    await _call(
        hass,
        "set_volume",
        {
            "target": "system_output",
            "amount": 30,
            "device": "alsa_output.usb",
            "target_peer_id": "peer-1",
        },
    )
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": "machine_audio_volume_set",
        "target_connection_id": "",
        "value": "30",
        "action_params": {"audio_device": "alsa_output.usb"},
        "target_peer_id": "peer-1",
    }


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"target": "obs_input", "amount": -6}, "`input`"),
        ({"target": "obs_input", "input": "  ", "amount": -6}, "`input`"),
        ({"target": "obs_input", "input": "Mic", "amount": 1, "device": "x"}, "`device`"),
        ({"target": "system_output", "amount": 50, "unit": "db"}, "percent, not dB"),
        ({"target": "system_output", "amount": 50, "input": "Mic"}, "`input`"),
        ({"target": "system_output", "amount": 50, "connection": "Home OBS"}, "`connection`"),
        ({"target": "system_output"}, "`amount` is required"),
        ({"target": "obs_input", "input": "Mic", "mode": "up", "amount": -3}, "positive"),
        ({"target": "obs_input", "input": "Mic", "amount": -5, "unit": "percent"}, "0 and 100"),
        ({"target": "system_output", "amount": -5}, "0 and 100"),
        ({"target": "obs_input", "input": "Mic", "amount": 500}, None),
        ({"target": "speakers", "amount": 5}, None),
        ({"target": "obs_input", "input": "Mic", "mode": "max"}, None),
    ],
)
async def test_set_volume_refuses_a_call_the_app_would_misread(
    hass: HomeAssistant, data: dict, match: str | None
) -> None:
    entry = await _setup(hass)
    with pytest.raises(vol.Invalid, match=match):
        await _call(hass, "set_volume", data)
    assert _client(hass, entry.entry_id).commands == []


async def test_set_volume_says_why_the_app_refused(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    _client(hass, entry.entry_id).error = SwitchboardApiError(
        "command failed: HTTP 400 this output has no volume control"
    )
    with pytest.raises(HomeAssistantError, match="no volume control.*2026-10-21"):
        await _call(hass, "set_volume", {"target": "system_output", "amount": 40})


@pytest.mark.parametrize(
    ("direction", "action_type"),
    [("next", "spotify_next"), ("previous", "spotify_previous")],
)
async def test_spotify_skip_sends_the_spotify_sentinel(
    hass: HomeAssistant, direction: str, action_type: str
) -> None:
    entry = await _setup(hass)
    await _call(hass, "spotify_skip", {"direction": direction})
    assert _client(hass, entry.entry_id).commands[-1] == {
        "action_type": action_type,
        "target_connection_id": "spotify",
        "value": "",
    }


async def test_spotify_skip_validates_and_explains(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    with pytest.raises(vol.Invalid):
        await _call(hass, "spotify_skip", {"direction": "sideways"})
    with pytest.raises(vol.Invalid):
        await _call(hass, "spotify_skip", {})
    assert _client(hass, entry.entry_id).commands == []
    _client(hass, entry.entry_id).error = SwitchboardApiError(
        "command failed: HTTP 400 unknown action_type"
    )
    with pytest.raises(HomeAssistantError, match="2026-10-21"):
        await _call(hass, "spotify_skip", {"direction": "next"})

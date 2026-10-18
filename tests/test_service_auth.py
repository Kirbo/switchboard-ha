"""SB-00231: every switchboard.* service is admin-only unless the entry allows non-admin users.

Each service runs with the entry's Switchboard API token — usually the global one, which reaches
`ha_service_call` back into Home Assistant with the token Switchboard holds — so a non-admin HA
user (a household member, a wall tablet) must not get it by default. The semantics mirror Home
Assistant's own `async_register_admin_service`: no user on the context → allowed, an unknown user
→ `UnknownUser`, a non-admin → `Unauthorized`.
"""

from __future__ import annotations

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized, UnknownUser
import pytest
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.switchboard.const import CONF_ALLOW_NON_ADMIN_SERVICES, DOMAIN

from .test_entities import _setup

ALLOW = {CONF_ALLOW_NON_ADMIN_SERVICES: True}

# Minimal valid data for EVERY registered service; the coverage test below fails when a service
# is added without an entry here, so a new one cannot ship untested against the admin check.
SERVICE_DATA: dict[str, dict] = {
    "run_action": {"action_type": "obs_scene_set", "target": "Home OBS", "value": "BRB"},
    "obs_scene_set": {"target": "Home OBS", "scene": "BRB"},
    "apply_scene_stream_info": {"target": "Home OBS"},
    "go_live": {"account_id": "Main"},
    "overlay_alert": {"text": "hi"},
    "set_machine_state": {"state": "afk"},
    "light_flash": {"entity_id": "light.desk"},
    "afk_snooze": {"seconds": 60},
    "afk_reset_idle": {},
    "set_input_overlay_visible": {},
    "set_variable": {"variable": "deaths", "value": "1"},
    "add_to_variable": {"variable": "deaths"},
    "alert_skip": {},
    "alert_pause": {},
    "alert_resume": {},
    "alert_replay": {},
}


def _commands(hass: HomeAssistant, entry_id: str) -> list[dict]:
    return hass.data[DOMAIN][entry_id].client.commands


async def _call(
    hass: HomeAssistant, service: str, data: dict, user: MockUser | None = None
) -> None:
    context = Context(user_id=user.id) if user is not None else None
    await hass.services.async_call(DOMAIN, service, data, blocking=True, context=context)


async def test_every_service_is_covered(hass: HomeAssistant) -> None:
    await _setup(hass)
    assert set(hass.services.async_services()[DOMAIN]) == set(SERVICE_DATA)


@pytest.mark.parametrize("service", sorted(SERVICE_DATA))
async def test_a_non_admin_user_is_refused_every_service_by_default(
    hass: HomeAssistant, hass_read_only_user: MockUser, service: str
) -> None:
    entry = await _setup(hass)
    with pytest.raises(Unauthorized):
        await _call(hass, service, SERVICE_DATA[service], hass_read_only_user)
    assert _commands(hass, entry.entry_id) == [], "nothing may reach Switchboard"


@pytest.mark.parametrize("service", sorted(SERVICE_DATA))
async def test_a_non_admin_user_may_call_every_service_when_the_option_is_on(
    hass: HomeAssistant, hass_read_only_user: MockUser, service: str
) -> None:
    entry = await _setup(hass, options=ALLOW)
    await _call(hass, service, SERVICE_DATA[service], hass_read_only_user)
    assert len(_commands(hass, entry.entry_id)) == 1


async def test_an_admin_user_is_allowed_with_the_option_off(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    entry = await _setup(hass)
    await _call(hass, "run_action", SERVICE_DATA["run_action"], hass_admin_user)
    assert _commands(hass, entry.entry_id)[-1]["action_type"] == "obs_scene_set"


async def test_a_call_without_a_user_is_allowed_with_the_option_off(hass: HomeAssistant) -> None:
    """Automations and the system call with no user on the context — like HA's admin services,
    those are never blocked."""
    entry = await _setup(hass)
    await _call(hass, "run_action", SERVICE_DATA["run_action"])
    assert _commands(hass, entry.entry_id)[-1]["action_type"] == "obs_scene_set"


async def test_an_unknown_user_is_refused(hass: HomeAssistant) -> None:
    entry = await _setup(hass, options=ALLOW)
    with pytest.raises(UnknownUser):
        await hass.services.async_call(
            DOMAIN,
            "run_action",
            SERVICE_DATA["run_action"],
            blocking=True,
            context=Context(user_id="no-such-user"),
        )
    assert _commands(hass, entry.entry_id) == []


async def test_the_option_is_read_at_call_time(
    hass: HomeAssistant, hass_read_only_user: MockUser
) -> None:
    """The check reads `entry.options` per call: toggling the option needs no reload."""
    entry = await _setup(hass)
    with pytest.raises(Unauthorized):
        await _call(hass, "run_action", SERVICE_DATA["run_action"], hass_read_only_user)

    hass.config_entries.async_update_entry(entry, options=ALLOW)
    await _call(hass, "run_action", SERVICE_DATA["run_action"], hass_read_only_user)
    assert len(_commands(hass, entry.entry_id)) == 1

    hass.config_entries.async_update_entry(entry, options={})
    with pytest.raises(Unauthorized):
        await _call(hass, "run_action", SERVICE_DATA["run_action"], hass_read_only_user)
    assert len(_commands(hass, entry.entry_id)) == 1


async def test_several_instances_need_the_option_on_every_one_a_call_can_reach(
    hass: HomeAssistant, hass_read_only_user: MockUser
) -> None:
    """Services are domain-wide, the option is per entry. Without `entry_id` the target may
    resolve on ANY instance, so every loaded one must allow it; naming an `entry_id` needs only
    that entry to."""
    allowing = await _setup(hass, options=ALLOW)
    strict = await _setup(hass)

    with pytest.raises(Unauthorized):
        await _call(hass, "run_action", SERVICE_DATA["run_action"], hass_read_only_user)
    with pytest.raises(Unauthorized):
        await _call(
            hass,
            "run_action",
            {**SERVICE_DATA["run_action"], "entry_id": strict.entry_id},
            hass_read_only_user,
        )
    assert _commands(hass, allowing.entry_id) == []
    assert _commands(hass, strict.entry_id) == []

    await _call(
        hass,
        "run_action",
        {**SERVICE_DATA["run_action"], "entry_id": allowing.entry_id},
        hass_read_only_user,
    )
    assert len(_commands(hass, allowing.entry_id)) == 1
    assert _commands(hass, strict.entry_id) == []

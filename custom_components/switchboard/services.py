"""Services that map to Switchboard's `POST /api/command` action executor.

A generic `run_action` passthrough (forward-compatible with the additive action list in
docs/HA.md) plus two conveniences. Targets accept a friendly connection label or a raw id;
anything that doesn't resolve to a known connection is passed through unchanged (so action
sentinels like `spotify`, or ids from a not-yet-refreshed list, still work — the backend
validates and 400s if truly wrong).
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .api import SwitchboardApiError
from .const import DOMAIN

SERVICE_RUN_ACTION = "run_action"
SERVICE_OBS_SCENE_SET = "obs_scene_set"
SERVICE_OVERLAY_ALERT = "overlay_alert"
SERVICE_SET_MACHINE_STATE = "set_machine_state"

ATTR_ACTION_TYPE = "action_type"
ATTR_TARGET = "target"
ATTR_VALUE = "value"
ATTR_ACTION_PARAMS = "action_params"
ATTR_SCENE = "scene"
ATTR_TEXT = "text"
ATTR_STATE = "state"

RUN_ACTION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ACTION_TYPE): cv.string,
        vol.Optional(ATTR_TARGET, default=""): cv.string,
        vol.Optional(ATTR_VALUE, default=""): cv.string,
        vol.Optional(ATTR_ACTION_PARAMS, default=dict): dict,
    }
)

OBS_SCENE_SET_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TARGET): cv.string,
        vol.Required(ATTR_SCENE): cv.string,
    }
)

OVERLAY_ALERT_SCHEMA = vol.Schema({vol.Required(ATTR_TEXT): cv.string})

SET_MACHINE_STATE_SCHEMA = vol.Schema({vol.Required(ATTR_STATE): vol.In(["afk", "active"])})


def _coordinators(hass: HomeAssistant) -> list[Any]:
    return list(hass.data.get(DOMAIN, {}).values())


def _pick(hass: HomeAssistant, target: str) -> tuple[Any, str]:
    """Choose a coordinator + resolve the target to a connection id (or pass through)."""
    coords = _coordinators(hass)
    if not coords:
        raise HomeAssistantError("Switchboard is not set up")
    if target:
        for coord in coords:
            try:
                return coord, coord.resolve_connection_id(target)
            except ValueError:
                continue
        # Unknown to every instance — treat as a literal sentinel/id on the first one.
        return coords[0], target
    return coords[0], ""


async def _send(coord: Any, payload: dict[str, Any]) -> None:
    try:
        result = await coord.client.send_command(payload)
    except SwitchboardApiError as err:
        raise HomeAssistantError(f"Switchboard command failed: {err}") from err
    if not result.get("ok"):
        raise HomeAssistantError(f"Switchboard rejected the command: {result}")


def async_register_services(hass: HomeAssistant) -> None:
    """Register domain services once (idempotent across multiple config entries)."""
    if hass.services.has_service(DOMAIN, SERVICE_RUN_ACTION):
        return

    async def handle_run_action(call: ServiceCall) -> None:
        coord, target_id = _pick(hass, call.data[ATTR_TARGET])
        await _send(
            coord,
            {
                "action_type": call.data[ATTR_ACTION_TYPE],
                "target_connection_id": target_id,
                "value": call.data[ATTR_VALUE],
                "action_params": call.data[ATTR_ACTION_PARAMS],
            },
        )

    async def handle_obs_scene_set(call: ServiceCall) -> None:
        coord, target_id = _pick(hass, call.data[ATTR_TARGET])
        await _send(
            coord,
            {
                "action_type": "obs_scene_set",
                "target_connection_id": target_id,
                "value": call.data[ATTR_SCENE],
            },
        )

    async def handle_overlay_alert(call: ServiceCall) -> None:
        coord, _ = _pick(hass, "")
        await _send(
            coord,
            {"action_type": "overlay_alert_show", "value": call.data[ATTR_TEXT]},
        )

    async def handle_set_machine_state(call: ServiceCall) -> None:
        coord, _ = _pick(hass, "")
        await _send(
            coord,
            {"action_type": "machine_state_set", "value": call.data[ATTR_STATE]},
        )

    hass.services.async_register(
        DOMAIN, SERVICE_RUN_ACTION, handle_run_action, schema=RUN_ACTION_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_OBS_SCENE_SET, handle_obs_scene_set, schema=OBS_SCENE_SET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_OVERLAY_ALERT, handle_overlay_alert, schema=OVERLAY_ALERT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_MACHINE_STATE, handle_set_machine_state, schema=SET_MACHINE_STATE_SCHEMA
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove services when the last entry unloads."""
    for service in (
        SERVICE_RUN_ACTION,
        SERVICE_OBS_SCENE_SET,
        SERVICE_OVERLAY_ALERT,
        SERVICE_SET_MACHINE_STATE,
    ):
        hass.services.async_remove(DOMAIN, service)

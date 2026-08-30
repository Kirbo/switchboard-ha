"""Services that map to Switchboard's `POST /api/command` action executor.

A generic `run_action` passthrough (forward-compatible with the additive action list in
docs/HA.md) plus typed conveniences for the actions worth a proper UI: `obs_scene_set`,
`twitch_go_live`, `overlay_alert_show`, `machine_state_set`, `ha_light_flash`, `afk_snooze`,
`afk_reset_idle`.
`ha_light_flash` (like `ha_service_call` and `discord_webhook_send`) requires the **global**
External API token — a scope-limited plugin token cannot call it.

Targets accept a friendly connection label or a raw id;
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
SERVICE_GO_LIVE = "go_live"
SERVICE_OVERLAY_ALERT = "overlay_alert"
SERVICE_SET_MACHINE_STATE = "set_machine_state"
SERVICE_LIGHT_FLASH = "light_flash"
SERVICE_AFK_SNOOZE = "afk_snooze"
SERVICE_AFK_RESET_IDLE = "afk_reset_idle"
SERVICE_SET_VARIABLE = "set_variable"
SERVICE_ADD_TO_VARIABLE = "add_to_variable"

ATTR_ACTION_TYPE = "action_type"
ATTR_TARGET = "target"
ATTR_ACCOUNT_ID = "account_id"
ATTR_OBS_ID = "obs_id"
ATTR_VALUE = "value"
ATTR_ACTION_PARAMS = "action_params"
ATTR_SCENE = "scene"
ATTR_TEXT = "text"
ATTR_STATE = "state"
ATTR_ENTRY_ID = "entry_id"
ATTR_ENTITY_ID = "entity_id"
ATTR_FLASHES = "flashes"
ATTR_COLOR = "color"
ATTR_BRIGHTNESS = "brightness"
ATTR_VARIABLE = "variable"
ATTR_AMOUNT = "amount"
ATTR_COLOR_TEMP_KELVIN = "color_temp_kelvin"
ATTR_ON_MS = "on_ms"
ATTR_OFF_MS = "off_ms"
ATTR_TRANSITION_MS = "transition_ms"
ATTR_SECONDS = "seconds"

# Every service takes an optional entry_id so a specific Switchboard instance can be addressed
# when several machines are configured (without it, the first entry wins).
_ENTRY_FIELD = {vol.Optional(ATTR_ENTRY_ID, default=""): cv.string}

RUN_ACTION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ACTION_TYPE): cv.string,
        vol.Optional(ATTR_TARGET, default=""): cv.string,
        vol.Optional(ATTR_VALUE, default=""): cv.string,
        vol.Optional(ATTR_ACTION_PARAMS, default=dict): dict,
        **_ENTRY_FIELD,
    }
)

OBS_SCENE_SET_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TARGET): cv.string,
        vol.Required(ATTR_SCENE): cv.string,
        **_ENTRY_FIELD,
    }
)

# `twitch_go_live` — composite (docs/HA.md Commands): fetch the account's stream key, set it on
# the target OBS AND start its stream as one operation, then make the account Switchboard's
# default. The key itself never rides any request/response/event — this payload is ids only.
GO_LIVE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ACCOUNT_ID): cv.string,
        vol.Optional(ATTR_OBS_ID, default=""): cv.string,
        **_ENTRY_FIELD,
    }
)

OVERLAY_ALERT_SCHEMA = vol.Schema({vol.Required(ATTR_TEXT): cv.string, **_ENTRY_FIELD})

SET_MACHINE_STATE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_STATE): vol.In(["afk", "active"]), **_ENTRY_FIELD}
)

# Switchboard user variables (counters, mode flags). The name rides action_params.variable; the
# value/amount is the command's `value`. Switchboard lowercases the name and creates the variable
# on first write, so no setup step is needed from here.
SET_VARIABLE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_VARIABLE): cv.string, vol.Required(ATTR_VALUE): cv.string, **_ENTRY_FIELD}
)

ADD_TO_VARIABLE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_VARIABLE): cv.string,
        vol.Optional(ATTR_AMOUNT, default=1): vol.Coerce(float),
        **_ENTRY_FIELD,
    }
)

# `ha_light_flash` — everything rides in action_params.ha_flash; only `entity_id` is required.
# Ranges mirror the clamps the app applies at perform time (docs/HA.md Commands).
LIGHT_FLASH_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.string,
        vol.Optional(ATTR_TARGET, default=""): cv.string,
        vol.Optional(ATTR_FLASHES): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
        vol.Optional(ATTR_COLOR): cv.string,
        vol.Optional(ATTR_BRIGHTNESS): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
        vol.Optional(ATTR_COLOR_TEMP_KELVIN): vol.All(
            vol.Coerce(int), vol.Range(min=2000, max=6500)
        ),
        vol.Optional(ATTR_ON_MS): vol.All(vol.Coerce(int), vol.Range(min=100, max=5000)),
        vol.Optional(ATTR_OFF_MS): vol.All(vol.Coerce(int), vol.Range(min=100, max=5000)),
        vol.Optional(ATTR_TRANSITION_MS): vol.All(vol.Coerce(int), vol.Range(min=0, max=5000)),
        **_ENTRY_FIELD,
    }
)

# 0 cancels an active snooze; the app caps the accumulated window at 4 h.
AFK_SNOOZE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SECONDS): vol.All(vol.Coerce(int), vol.Range(min=0, max=4 * 60 * 60)),
        **_ENTRY_FIELD,
    }
)

AFK_RESET_IDLE_SCHEMA = vol.Schema(dict(_ENTRY_FIELD))

# ha_flash keys forwarded verbatim when supplied (the app defaults anything omitted).
_FLASH_KEYS = (
    ATTR_FLASHES,
    ATTR_COLOR,
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_ON_MS,
    ATTR_OFF_MS,
    ATTR_TRANSITION_MS,
)


def _pick(hass: HomeAssistant, target: str, entry_id: str = "") -> tuple[Any, str]:
    """Choose a coordinator + resolve the target to a connection id (or pass through)."""
    domain_data = hass.data.get(DOMAIN, {})
    if entry_id:
        coord = domain_data.get(entry_id)
        if coord is None:
            raise HomeAssistantError(f"no Switchboard config entry with id '{entry_id}'")
        coords = [coord]
    else:
        coords = list(domain_data.values())
    if not coords:
        raise HomeAssistantError("Switchboard is not set up")
    if target:
        ambiguous: ValueError | None = None
        for coord in coords:
            try:
                resolved = coord.resolve_connection_id(target)
            except ValueError as err:
                # Ambiguous label on this instance — remember the actionable message instead of
                # silently posting the raw label as a connection id (an opaque backend 400).
                ambiguous = err
                continue
            if resolved is not None:
                return coord, resolved
        if ambiguous is not None:
            raise HomeAssistantError(str(ambiguous)) from ambiguous
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
    """Register domain services (called once from async_setup; guarded for safety)."""
    if hass.services.has_service(DOMAIN, SERVICE_RUN_ACTION):
        return

    async def handle_run_action(call: ServiceCall) -> None:
        coord, target_id = _pick(hass, call.data[ATTR_TARGET], call.data[ATTR_ENTRY_ID])
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
        coord, target_id = _pick(hass, call.data[ATTR_TARGET], call.data[ATTR_ENTRY_ID])
        await _send(
            coord,
            {
                "action_type": "obs_scene_set",
                "target_connection_id": target_id,
                "value": call.data[ATTR_SCENE],
            },
        )

    async def handle_go_live(call: ServiceCall) -> None:
        coord, account_id = _pick(hass, call.data[ATTR_ACCOUNT_ID], call.data[ATTR_ENTRY_ID])
        payload: dict[str, Any] = {
            "action_type": "twitch_go_live",
            "target_connection_id": account_id,
            "value": "",
        }
        if obs := call.data[ATTR_OBS_ID]:
            # Label-or-id like every other target; an unknown value passes through unchanged for
            # the app to validate. Omitted entirely → no action_params, so the app resolves its
            # default local OBS (docs/HA.md: both target keys absent = the default local OBS).
            try:
                resolved = coord.resolve_connection_id(obs, "obs")
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
            payload["action_params"] = {"obs_id": resolved or obs}
        await _send(coord, payload)

    async def handle_overlay_alert(call: ServiceCall) -> None:
        coord, _ = _pick(hass, "", call.data[ATTR_ENTRY_ID])
        await _send(
            coord,
            {"action_type": "overlay_alert_show", "value": call.data[ATTR_TEXT]},
        )

    async def handle_set_machine_state(call: ServiceCall) -> None:
        coord, _ = _pick(hass, "", call.data[ATTR_ENTRY_ID])
        await _send(
            coord,
            {"action_type": "machine_state_set", "value": call.data[ATTR_STATE]},
        )
        coord.schedule_afk_refresh()

    async def handle_set_variable(call: ServiceCall) -> None:
        coord, _ = _pick(hass, "", call.data[ATTR_ENTRY_ID])
        await _send(
            coord,
            {
                "action_type": "var_set",
                "value": call.data[ATTR_VALUE],
                "action_params": {"variable": call.data[ATTR_VARIABLE]},
            },
        )

    async def handle_add_to_variable(call: ServiceCall) -> None:
        coord, _ = _pick(hass, "", call.data[ATTR_ENTRY_ID])
        await _send(
            coord,
            {
                "action_type": "var_add",
                # Switchboard parses the amount itself; send it as the plain number it is.
                "value": str(call.data[ATTR_AMOUNT]),
                "action_params": {"variable": call.data[ATTR_VARIABLE]},
            },
        )

    async def handle_light_flash(call: ServiceCall) -> None:
        coord, target_id = _pick(hass, call.data[ATTR_TARGET], call.data[ATTR_ENTRY_ID])
        if not target_id:
            # The HA connection is optional in the service: with one configured (or one flagged
            # default) the app's own smart-default applies, so resolve it here for a zero-click
            # call and only complain when the choice is genuinely ambiguous.
            target_id = coord.default_id("home_assistant") or ""
            if not target_id:
                raise HomeAssistantError(
                    "no default Home Assistant connection in Switchboard — pass `target` with "
                    "the connection label or id"
                )
        flash = {ATTR_ENTITY_ID: call.data[ATTR_ENTITY_ID]}
        flash.update({k: call.data[k] for k in _FLASH_KEYS if k in call.data})
        await _send(
            coord,
            {
                "action_type": "ha_light_flash",
                "target_connection_id": target_id,
                "value": "",
                "action_params": {"ha_flash": flash},
            },
        )

    async def handle_afk_snooze(call: ServiceCall) -> None:
        coord, _ = _pick(hass, "", call.data[ATTR_ENTRY_ID])
        await _send(
            coord,
            {"action_type": "afk_snooze", "value": str(call.data[ATTR_SECONDS])},
        )
        coord.schedule_afk_refresh()

    async def handle_afk_reset_idle(call: ServiceCall) -> None:
        coord, _ = _pick(hass, "", call.data[ATTR_ENTRY_ID])
        await _send(coord, {"action_type": "afk_reset_idle", "value": ""})
        coord.schedule_afk_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_RUN_ACTION, handle_run_action, schema=RUN_ACTION_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_OBS_SCENE_SET, handle_obs_scene_set, schema=OBS_SCENE_SET_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_GO_LIVE, handle_go_live, schema=GO_LIVE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_OVERLAY_ALERT, handle_overlay_alert, schema=OVERLAY_ALERT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_MACHINE_STATE, handle_set_machine_state, schema=SET_MACHINE_STATE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_VARIABLE, handle_set_variable, schema=SET_VARIABLE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_TO_VARIABLE, handle_add_to_variable, schema=ADD_TO_VARIABLE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LIGHT_FLASH, handle_light_flash, schema=LIGHT_FLASH_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_AFK_SNOOZE, handle_afk_snooze, schema=AFK_SNOOZE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_AFK_RESET_IDLE, handle_afk_reset_idle, schema=AFK_RESET_IDLE_SCHEMA
    )

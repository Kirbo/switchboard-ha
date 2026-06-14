"""Config flow for Switchboard."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, CONF_VERIFY_SSL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import SwitchboardApiError, SwitchboardAuthError, SwitchboardClient
from .const import CONF_FINGERPRINT, DEFAULT_PORT, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_TOKEN): str,
        vol.Required(CONF_VERIFY_SSL, default=False): bool,
        vol.Optional(CONF_FINGERPRINT, default=""): str,
    }
)


class SwitchboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle adding a Switchboard instance."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            client = SwitchboardClient(
                async_get_clientsession(self.hass),
                host,
                port,
                user_input[CONF_TOKEN],
                verify_ssl=user_input[CONF_VERIFY_SSL],
                fingerprint=user_input.get(CONF_FINGERPRINT) or None,
            )
            try:
                await client.fetch_state()
            except SwitchboardAuthError:
                errors["base"] = "invalid_auth"
            except SwitchboardApiError:
                errors["base"] = "cannot_connect"
            except ValueError:
                # bad fingerprint hex
                errors["base"] = "bad_fingerprint"
            else:
                return self.async_create_entry(title=f"Switchboard ({host})", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input or {}),
            errors=errors,
        )

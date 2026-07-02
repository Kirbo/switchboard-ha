"""Config flow for Switchboard: initial setup, reauth (token), and reconfigure."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, CONF_VERIFY_SSL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import (
    SwitchboardAccessError,
    SwitchboardApiError,
    SwitchboardAuthError,
    SwitchboardClient,
)
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

STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): str})


class SwitchboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle adding, reauthenticating, and reconfiguring a Switchboard instance."""

    VERSION = 1

    async def _async_validate(self, data: dict[str, Any]) -> str | None:
        """Try one authenticated call with the given config; return an error key or None.

        The client is constructed INSIDE the try: a malformed fingerprint raises ValueError
        from `bytes.fromhex` during construction, before any request is made.
        """
        try:
            client = SwitchboardClient(
                async_get_clientsession(self.hass),
                data[CONF_HOST],
                data[CONF_PORT],
                data[CONF_TOKEN],
                verify_ssl=data.get(CONF_VERIFY_SSL, False),
                fingerprint=data.get(CONF_FINGERPRINT) or None,
            )
            await client.fetch_state()
        except SwitchboardAuthError:
            return "invalid_auth"
        except SwitchboardAccessError:
            return "access_denied"
        except SwitchboardApiError:
            return "cannot_connect"
        except ValueError:
            return "bad_fingerprint"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            error = await self._async_validate(user_input)
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(title=f"Switchboard ({host})", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """The stored token stopped working (revoked/rotated) — ask for a new one."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, CONF_TOKEN: user_input[CONF_TOKEN]}
            error = await self._async_validate(data)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change host/port/token/TLS settings without deleting and re-adding the entry."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            # Moving to a different instance must not collide with another entry (the entry
            # being reconfigured is ignored by this check).
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            error = await self._async_validate(user_input)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=f"{host}:{port}",
                    title=f"Switchboard ({host})",
                    data=user_input,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or dict(entry.data)
            ),
            errors=errors,
        )

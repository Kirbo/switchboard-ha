"""Config flow for Switchboard: initial setup, reauth (token), and reconfigure."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, CONF_VERIFY_SSL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    SwitchboardAccessError,
    SwitchboardApiError,
    SwitchboardAuthError,
    SwitchboardClient,
)
from .const import CONF_FINGERPRINT, DEFAULT_PORT, DOMAIN

# The token is a PASSWORD field, not plain text (SB-A-059). Home Assistant renders a plain `str`
# in the clear, and `add_suggested_values_to_schema` below pre-fills it with the STORED value — so
# opening Reconfigure, a normal thing to do while debugging, painted the Switchboard API token on
# screen in full. That token drives every /api/command action, including ha_service_call against
# the streamer's own house. `diagnostics.py` already redacts it; this form did not.
_TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_TOKEN): _TOKEN_SELECTOR,
        vol.Required(CONF_VERIFY_SSL, default=False): bool,
        vol.Optional(CONF_FINGERPRINT, default=""): str,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): _TOKEN_SELECTOR})


class SwitchboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle adding, reauthenticating, and reconfiguring a Switchboard instance."""

    VERSION = 1

    async def _async_validate(self, data: dict[str, Any]) -> str | None:
        """Try one authenticated call with the given config; return an error key or None.

        The client is constructed INSIDE the try: a malformed fingerprint raises ValueError
        from `bytes.fromhex` during construction, before any request is made.
        """
        # SB-A-061: refuse a configuration with NEITHER TLS verification NOR a pinned fingerprint.
        # That combination — which used to be the shipped default — means aiohttp is handed
        # `ssl=False`, so any host that can ARP-spoof the segment terminates the connection with its
        # own certificate and reads `Authorization: Bearer <token>` off the very first request. On a
        # network that also runs IoT devices, that is a realistic attacker. Pin the fingerprint
        # (shown on Switchboard's Peers tab) or front the app with a trusted certificate.
        if not data.get(CONF_VERIFY_SSL, False) and not (data.get(CONF_FINGERPRINT) or "").strip():
            return "no_tls_trust"
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
            # Moving to a different instance must not collide with ANOTHER entry — but the entry
            # being reconfigured obviously already owns its own host:port.
            #
            # `_abort_if_unique_id_configured()` cannot express that: it looks the unique_id up with
            # `async_entry_for_domain_unique_id` and raises AbortFlow unconditionally, with no
            # exclusion for the entry this flow is reconfiguring. So every reconfigure that kept the
            # same host and port aborted with "already_configured" and saved NOTHING, while the
            # dialog closed exactly like a success — which is how an entry could sit there insisting
            # its fingerprint was unset after the user had just set it.
            await self.async_set_unique_id(f"{host}:{port}")
            for other in self._async_current_entries(include_ignore=False):
                if other.entry_id != entry.entry_id and other.unique_id == self.unique_id:
                    return self.async_abort(reason="already_configured")

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
            # Everything EXCEPT the token is pre-filled. Suggesting the stored token would send it
            # back down to the browser on every Reconfigure — the exact exposure the password
            # selector above is there to prevent. Leaving it blank means "re-enter it", which is
            # the right prompt for a credential.
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                {k: v for k, v in (user_input or dict(entry.data)).items() if k != CONF_TOKEN},
            ),
            errors=errors,
        )

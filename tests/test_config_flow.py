"""Config-flow guarantees that are security-relevant, not cosmetic.

Both came out of the Switchboard audit (2026-08-31): the token was rendered in the clear on the
Reconfigure form, and the shipped default was a connection that could not be authenticated at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, CONF_VERIFY_SSL
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.switchboard.config_flow import (
    STEP_REAUTH_SCHEMA,
    STEP_USER_SCHEMA,
    SwitchboardConfigFlow,
)
from custom_components.switchboard.const import CONF_FINGERPRINT, DOMAIN

from .conftest import FakeClient

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "switchboard"
FINGERPRINT = "a" * 64


def _selector_for(schema, key):
    for k, v in schema.schema.items():
        if k == key:
            return v
    raise AssertionError(f"{key} missing from schema")


def test_the_token_field_is_a_password_selector_not_plain_text():
    """SB-A-059: a plain `str` renders in the clear, and Reconfigure pre-fills the stored value —
    so opening that form put the full API token on screen. That token drives every /api/command,
    including ha_service_call against the user's own house."""
    for schema in (STEP_USER_SCHEMA, STEP_REAUTH_SCHEMA):
        selector = _selector_for(schema, CONF_TOKEN)
        assert not isinstance(selector, type), "token must not be a bare `str` type"
        config = getattr(selector, "config", {})
        assert str(config.get("type")).endswith("password"), (
            f"token selector is {config.get('type')!r}, expected a password selector"
        )


@pytest.mark.asyncio
async def test_a_connection_with_no_way_to_authenticate_tls_is_refused():
    """SB-A-061: verify_ssl off AND no fingerprint means aiohttp gets ssl=False, so anything that
    can ARP-spoof the segment reads the bearer token off the first request. That was the default."""
    flow = SwitchboardConfigFlow()
    base = {"host": "192.168.1.50", "port": 38474, CONF_TOKEN: "t"}

    assert (
        await flow._async_validate({**base, CONF_VERIFY_SSL: False, CONF_FINGERPRINT: ""})
        == "no_tls_trust"
    )
    assert await flow._async_validate({**base, CONF_VERIFY_SSL: False}) == "no_tls_trust", (
        "an absent fingerprint key must be treated the same as an empty one"
    )
    # Whitespace is not a pin.
    assert (
        await flow._async_validate({**base, CONF_VERIFY_SSL: False, CONF_FINGERPRINT: "   "})
        == "no_tls_trust"
    )


def test_the_refusal_has_a_user_facing_message():
    """An error key with no string renders as a blank form error, which reads as a bug."""
    for name in ("strings.json", "translations/en.json"):
        text = json.loads((COMPONENT / name).read_text())
        assert "no_tls_trust" in json.dumps(text), f"{name} is missing the no_tls_trust message"


async def test_reconfigure_saves_when_host_and_port_are_unchanged(hass):
    """The reconfigure form must actually persist — the common case is changing ONLY the token or
    the fingerprint, leaving host and port alone.

    This shipped broken. `_abort_if_unique_id_configured()` resolves the unique_id with
    `async_entry_for_domain_unique_id` and raises AbortFlow unconditionally; it has no exclusion for
    the entry the flow is reconfiguring. So an unchanged host:port aborted with
    "already_configured" and wrote nothing, while the dialog closed exactly like a success. A user
    pinning a fingerprint to clear the insecure-TLS gate watched the entry keep failing with "no TLS
    fingerprint is pinned" — the value they had just typed never reached `entry.data`.

    The other config-flow tests here assert on the SCHEMA, so none of them drove the flow and none
    could see it.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.56:38474",
        data={
            CONF_HOST: "192.168.1.56",
            CONF_PORT: 38474,
            CONF_TOKEN: "old-token",
            CONF_VERIFY_SSL: False,
            CONF_FINGERPRINT: "",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM

    # Validation is a live call, and a successful reconfigure reloads the entry (which builds a
    # real client). Both are stubbed: this test is about whether the new data is PERSISTED.
    client = FakeClient(state={}, connections=[], afk={})
    with (
        patch.object(SwitchboardConfigFlow, "_async_validate", return_value=None),
        patch("custom_components.switchboard.SwitchboardClient", return_value=client),
        patch(
            "custom_components.switchboard.coordinator.SwitchboardCoordinator.async_start",
            return_value=None,
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.1.56",
                CONF_PORT: 38474,
                CONF_TOKEN: "new-token",
                CONF_VERIFY_SSL: False,
                CONF_FINGERPRINT: FINGERPRINT,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful", (
        f"reconfigure aborted with {result['reason']!r} instead of saving"
    )
    assert entry.data[CONF_FINGERPRINT] == FINGERPRINT
    assert entry.data[CONF_TOKEN] == "new-token"

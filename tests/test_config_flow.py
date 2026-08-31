"""Config-flow guarantees that are security-relevant, not cosmetic.

Both came out of the Switchboard audit (2026-08-31): the token was rendered in the clear on the
Reconfigure form, and the shipped default was a connection that could not be authenticated at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.const import CONF_TOKEN, CONF_VERIFY_SSL
import pytest

from custom_components.switchboard.config_flow import (
    STEP_REAUTH_SCHEMA,
    STEP_USER_SCHEMA,
    SwitchboardConfigFlow,
)
from custom_components.switchboard.const import CONF_FINGERPRINT

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

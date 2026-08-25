"""Static metadata consistency — the drift hassfest doesn't catch for custom integrations.

A service registered in code but missing from `services.yaml` has no UI; one described in
`strings.json` but not in `services.yaml` shows a blank field; and `translations/en.json` is a
verbatim copy of `strings.json` that silently rots when only one of them is edited.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import yaml

from custom_components.switchboard import services as services_mod
from custom_components.switchboard.const import DOMAIN

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "switchboard"


def _services_yaml() -> dict:
    return yaml.safe_load((COMPONENT / "services.yaml").read_text())


def _strings() -> dict:
    return json.loads((COMPONENT / "strings.json").read_text())


def test_en_translation_matches_strings() -> None:
    assert json.loads((COMPONENT / "translations/en.json").read_text()) == _strings()


def test_every_registered_service_is_documented() -> None:
    registered = {
        getattr(services_mod, name)
        for name in dir(services_mod)
        if name.startswith("SERVICE_") and isinstance(getattr(services_mod, name), str)
    }
    assert registered == set(_services_yaml())
    assert registered == set(_strings()["services"])


def test_service_fields_match_between_yaml_and_strings() -> None:
    strings = _strings()["services"]
    for name, spec in _services_yaml().items():
        yaml_fields = set((spec or {}).get("fields") or {})
        string_fields = set(strings[name].get("fields") or {})
        assert yaml_fields == string_fields, name


def test_manifest_is_sane() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["domain"] == DOMAIN
    assert manifest["config_flow"] is True
    # The release script rewrites this to the CalVer tag; HACS shows the tag, HA reads the file.
    assert re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", manifest["version"]), manifest["version"]
    # Pure /api consumer: everything it needs (aiohttp) already ships with Home Assistant.
    assert manifest["requirements"] == []


def test_hacs_manifest_declares_a_minimum_ha() -> None:
    hacs = json.loads((COMPONENT.parent.parent / "hacs.json").read_text())
    assert hacs["name"]
    assert re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", hacs["homeassistant"])

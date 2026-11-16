"""Tests for manifest, HACS metadata, and integration constants."""

from __future__ import annotations

import json
from pathlib import Path

from dtek_client.const import DTEK_SITES

from custom_components.ua_outages.const import (
    DEFAULT_GROUPS,
    DOMAIN,
    EVENT_TYPES,
    GROUPS_MODES,
    LANGUAGES,
    MIRROR_URLS,
    SLOT_MAYBE,
    SLOT_OFF,
    SLOT_ON,
    SLOTS_PER_DAY,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
COMPONENT_DIR = ROOT_DIR / "custom_components" / DOMAIN


def test_manifest_is_valid() -> None:
    """Verify manifest.json contains required HA metadata and library pin."""
    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == DOMAIN
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_polling"
    assert "dtek-blackout-client==0.2.1" in manifest["requirements"]
    assert isinstance(manifest["version"], str) and manifest["version"]


def test_hacs_json_is_valid() -> None:
    """Verify hacs.json defines integration name and minimum HA version."""
    hacs = json.loads((ROOT_DIR / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"] == "Ukraine Power Outages"
    assert hacs["render_readme"] is True
    assert "homeassistant" in hacs


def test_mirror_urls_cover_all_dtek_sites() -> None:
    """Verify MIRROR_URLS explicitly maps every supported DTEK site_key."""
    assert set(MIRROR_URLS.keys()) == set(DTEK_SITES.keys())
    assert MIRROR_URLS["dem"] is None
    for site_key in ("kem", "krem", "dnem", "oem"):
        url = MIRROR_URLS[site_key]
        assert isinstance(url, str) and url.startswith("https://")


def test_constants_invariants() -> None:
    """Verify core schedule, event, and mode constants are internally consistent."""
    assert SLOTS_PER_DAY == 48
    assert len({SLOT_ON, SLOT_OFF, SLOT_MAYBE}) == 3
    assert len(EVENT_TYPES) == 10
    assert GROUPS_MODES == ("main", "any", "all")
    assert LANGUAGES == ("uk", "en", "ru")
    assert len(DEFAULT_GROUPS) == 12


def test_translations_and_icons_are_consistent() -> None:
    """Verify strings.json, all translations, and icons.json are valid and aligned."""
    strings = json.loads((COMPONENT_DIR / "strings.json").read_text(encoding="utf-8"))
    icons = json.loads((COMPONENT_DIR / "icons.json").read_text(encoding="utf-8"))

    for lang in ("en", "uk", "ru"):
        trans_path = COMPONENT_DIR / "translations" / f"{lang}.json"
        assert trans_path.exists(), f"Missing translation file: {trans_path}"
        trans = json.loads(trans_path.read_text(encoding="utf-8"))
        assert set(trans.keys()) == set(strings.keys())
        assert set(trans["config"]["step"].keys()) == set(strings["config"]["step"].keys())
        assert set(trans["config"]["error"].keys()) == set(strings["config"]["error"].keys())
        assert set(trans["entity"].keys()) == set(strings["entity"].keys())
        for platform, entities in strings["entity"].items():
            assert set(trans["entity"][platform].keys()) == set(entities.keys())

    assert set(icons["entity"].keys()) == set(strings["entity"].keys())
    assert set(icons["services"].keys()) == set(strings["services"].keys())

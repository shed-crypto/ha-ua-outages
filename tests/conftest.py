"""Shared pytest fixtures for ha-ua-outages integration tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ua_outages.const import (
    CONF_GROUP,
    CONF_HOUSE,
    CONF_LANGUAGE,
    CONF_MANUAL,
    CONF_MODE,
    CONF_SETTLEMENT,
    CONF_SITE_KEY,
    CONF_STREET,
    DEFAULT_LANGUAGE,
    DOMAIN,
    MODE_ADDRESS,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> Generator[None]:
    """Allow loading custom_components.ua_outages in all HA tests."""
    yield


@pytest.fixture
def address_entry() -> MockConfigEntry:
    """Return a configured address-mode ConfigEntry for krem."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Home (Ukrainka)",
        unique_id="krem|м. Українка|вул. Юності|1",
        data={
            CONF_SITE_KEY: "krem",
            CONF_MODE: MODE_ADDRESS,
            CONF_SETTLEMENT: "м. Українка",
            CONF_STREET: "вул. Юності",
            CONF_HOUSE: "1",
            CONF_GROUP: "GPV3.1",
            CONF_MANUAL: False,
        },
        options={CONF_LANGUAGE: DEFAULT_LANGUAGE},
    )

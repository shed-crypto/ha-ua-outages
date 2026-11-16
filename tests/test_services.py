"""Tests for integration setup/unload lifecycle and services."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ua_outages.api import ProviderSnapshot, Publication
from custom_components.ua_outages.const import (
    CONF_GROUP,
    CONF_MANUAL,
    CONF_MODE,
    CONF_SITE_KEY,
    DOMAIN,
    MODE_GROUP_ONLY,
    SLOT_OFF,
    SLOT_ON,
)
from custom_components.ua_outages.schedule import KYIV_TZ
from custom_components.ua_outages.services import (
    ATTR_CONFIG_ENTRY_ID,
    SERVICE_GET_SCHEDULE,
    SERVICE_REFRESH,
)


def _snap() -> ProviderSnapshot:
    return ProviderSnapshot(
        source="dtek",
        fact_update="16.11.2026 12:00",
        fact_updated_at=datetime(2026, 11, 16, 10, 0, tzinfo=UTC),
        today=datetime(2026, 11, 16, 12, 0, tzinfo=KYIV_TZ).date(),
        days_by_group={"GPV3.1": {"2026-11-16": (SLOT_OFF * 4) + (SLOT_ON * 44)}},
        preset_by_group={},
        sch_names={"GPV3.1": "Черга 3.1"},
        groups_at_address=["GPV3.1"],
        external_operator=False,
        address_outage=None,
        publication=Publication(True, True, True, True),
    )


class TestServicesAndLifecycle:
    @pytest.mark.freeze_time("2026-11-16T08:00:00+00:00")
    async def test_refresh_and_get_schedule_services(
        self,
        hass: HomeAssistant,
        address_entry: MockConfigEntry,
    ) -> None:
        address_entry.add_to_hass(hass)
        fetch_mock = AsyncMock(return_value=_snap())

        with patch("custom_components.ua_outages.coordinator.fetch_dtek_snapshot", new=fetch_mock):
            assert await hass.config_entries.async_setup(address_entry.entry_id)
            await hass.async_block_till_done()

            await hass.services.async_call(
                DOMAIN,
                SERVICE_REFRESH,
                {ATTR_CONFIG_ENTRY_ID: address_entry.entry_id},
                blocking=True,
            )
            assert fetch_mock.await_count >= 2

            resp = await hass.services.async_call(
                DOMAIN,
                SERVICE_GET_SCHEDULE,
                {},
                blocking=True,
                return_response=True,
            )
            assert resp is not None
            assert resp["site_key"] == "krem"
            assert resp["group_id"] == "GPV3.1"

            with pytest.raises(ServiceValidationError):
                await hass.services.async_call(
                    DOMAIN,
                    SERVICE_GET_SCHEDULE,
                    {ATTR_CONFIG_ENTRY_ID: "missing-entry"},
                    blocking=True,
                    return_response=True,
                )

        assert await hass.config_entries.async_unload(address_entry.entry_id)
        await hass.config_entries.async_remove(address_entry.entry_id)

    @pytest.mark.freeze_time("2026-11-16T08:00:00+00:00")
    async def test_cleanup_stale_address_entities_on_group_only(
        self,
        hass: HomeAssistant,
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Switched Entry",
            unique_id="krem|group|GPV3.1",
            data={
                CONF_SITE_KEY: "krem",
                CONF_MODE: MODE_GROUP_ONLY,
                CONF_GROUP: "GPV3.1",
                CONF_MANUAL: True,
            },
        )
        entry.add_to_hass(hass)

        registry = er.async_get(hass)
        stale = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_address_status",
            config_entry=entry,
        )

        with patch(
            "custom_components.ua_outages.coordinator.fetch_mirror_snapshot",
            new=AsyncMock(return_value=_snap()),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        assert registry.async_get(stale.entity_id) is None
        assert await hass.config_entries.async_unload(entry.entry_id)

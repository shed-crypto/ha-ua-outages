"""Tests for all 16 entities, translations, availability, calendars, events, and diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dtek_client.exceptions import DtekBlockedError
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ua_outages.api import AddressOutage, ProviderSnapshot, Publication
from custom_components.ua_outages.binary_sensor import (
    UaOutagesAddressOutageBinarySensor,
    UaOutagesScheduledOutageBinarySensor,
)
from custom_components.ua_outages.calendar import (
    UaOutagesMainCalendar,
    UaOutagesOtherGroupsCalendar,
)
from custom_components.ua_outages.const import (
    CONF_EXCLUDED_GROUPS,
    CONF_EXTRA_GROUPS,
    CONF_GROUP,
    CONF_GROUPS_MODE,
    CONF_LANGUAGE,
    CONF_MANUAL,
    CONF_MODE,
    CONF_SHOW_POSSIBLE,
    CONF_SITE_KEY,
    DOMAIN,
    EVENT_OUTAGE_STARTED,
    GROUPS_MODE_MAIN,
    MODE_GROUP_ONLY,
    OUTAGE_KIND_EMERGENCY,
    OUTAGE_KIND_STABILIZATION,
    SLOT_MAYBE,
    SLOT_OFF,
    SLOT_ON,
)
from custom_components.ua_outages.coordinator import UaOutagesCoordinator
from custom_components.ua_outages.diagnostics import async_get_config_entry_diagnostics
from custom_components.ua_outages.event import UaOutagesEventEntity
from custom_components.ua_outages.schedule import KYIV_TZ
from custom_components.ua_outages.sensor import (
    UaOutagesAddressStatusSensor,
    UaOutagesConnectionSensor,
    UaOutagesDailyHoursSensor,
    UaOutagesGroupSensor,
    UaOutagesGroupsStatusSensor,
    UaOutagesLastUpdateSensor,
    UaOutagesNextOutageSensor,
    UaOutagesOutageEndSensor,
    UaOutagesRestorationSensor,
    UaOutagesScheduleUpdatedSensor,
)


def _snapshot_with_outage(
    *,
    dtek_source: bool = True,
    outage: AddressOutage | None = None,
) -> ProviderSnapshot:
    today_slots = (
        (SLOT_ON * 20) + (SLOT_OFF * 4) + (SLOT_ON * 2) + (SLOT_MAYBE * 2) + (SLOT_ON * 20)
    )
    tomorrow_slots = (SLOT_OFF * 2) + (SLOT_ON * 46)
    return ProviderSnapshot(
        source="dtek" if dtek_source else "mirror",
        fact_update="16.11.2026 11:00",
        fact_updated_at=datetime(2026, 11, 16, 9, 0, tzinfo=UTC),
        today=datetime(2026, 11, 16, 11, 0, tzinfo=KYIV_TZ).date(),
        days_by_group={
            "GPV3.1": {"2026-11-16": today_slots, "2026-11-17": tomorrow_slots},
            "GPV3.2": {"2026-11-16": (SLOT_OFF * 24) + (SLOT_ON * 24)},
        },
        preset_by_group={
            "GPV3.1": {3: (SLOT_OFF * 2) + (SLOT_ON * 46)},
            "GPV3.2": {3: (SLOT_MAYBE * 2) + (SLOT_ON * 46)},
        },
        sch_names={"GPV3.1": "Черга 3.1", "GPV3.2": "Черга 3.2"},
        groups_at_address=["GPV3.1", "GPV3.2"],
        external_operator=True,
        address_outage=outage,
        publication=(
            Publication(
                daily_shown=True,
                weekly_shown=True,
                cur_schedule_shown=True,
                cur_outage_shown=True,
            )
            if dtek_source
            else None
        ),
    )


class TestEntitiesAndPlatforms:
    @pytest.mark.freeze_time("2026-11-16T08:30:00+00:00")
    async def test_all_16_entities_created_for_address_entry(
        self,
        hass: HomeAssistant,
        address_entry: MockConfigEntry,
    ) -> None:
        snap = _snapshot_with_outage(
            outage=AddressOutage(
                reason="Аварійне відключення",
                kind=OUTAGE_KIND_EMERGENCY,
                start=datetime(2026, 11, 16, 10, 0, tzinfo=KYIV_TZ),
                end=datetime(2026, 11, 16, 14, 0, tzinfo=KYIV_TZ),
                outage_type="2",
            )
        )
        address_entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(
            address_entry,
            options={
                CONF_LANGUAGE: "uk",
                CONF_GROUPS_MODE: GROUPS_MODE_MAIN,
                CONF_EXCLUDED_GROUPS: [],
                CONF_SHOW_POSSIBLE: True,
            },
        )

        with patch(
            "custom_components.ua_outages.coordinator.fetch_dtek_snapshot",
            new=AsyncMock(return_value=snap),
        ):
            assert await hass.config_entries.async_setup(address_entry.entry_id)
            await hass.async_block_till_done()

        registry = er.async_get(hass)
        entries = er.async_entries_for_config_entry(registry, address_entry.entry_id)
        assert len(entries) == 16

        # English entity_id suffixes stay fixed even when UI language is Ukrainian.
        entity_ids = {ent.entity_id for ent in entries}
        assert any(eid.endswith("_next_outage") for eid in entity_ids)
        assert any(eid.endswith("_outage_end") for eid in entity_ids)
        assert any(eid.endswith("_outages_tomorrow") for eid in entity_ids)
        assert any(eid.endswith("_address_status") for eid in entity_ids)
        assert any(eid.endswith("_events") for eid in entity_ids)

        group_state = hass.states.get(next(e for e in entity_ids if e.endswith("_group")))
        assert group_state is not None
        assert group_state.state == "3.1"
        assert group_state.attributes["site_key"] == "krem"
        assert group_state.attributes["external_operator"] is True

        addr_state = hass.states.get(next(e for e in entity_ids if e.endswith("_address_status")))
        assert addr_state is not None
        assert addr_state.state == OUTAGE_KIND_EMERGENCY

        assert await hass.config_entries.async_unload(address_entry.entry_id)

    @pytest.mark.freeze_time("2026-11-16T08:30:00+00:00")
    async def test_group_only_creates_13_entities_and_languages_switch(
        self,
        hass: HomeAssistant,
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Kyiv Group",
            unique_id="kem|group|GPV3.1",
            data={
                CONF_SITE_KEY: "kem",
                CONF_MODE: MODE_GROUP_ONLY,
                CONF_GROUP: "GPV3.1",
                CONF_MANUAL: True,
            },
            options={
                CONF_LANGUAGE: "en",
                CONF_EXTRA_GROUPS: ["GPV3.2"],
            },
        )
        entry.add_to_hass(hass)
        snap = _snapshot_with_outage(dtek_source=False)

        with patch(
            "custom_components.ua_outages.coordinator.fetch_mirror_snapshot",
            new=AsyncMock(return_value=snap),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        registry = er.async_get(hass)
        entries = er.async_entries_for_config_entry(registry, entry.entry_id)
        assert len(entries) == 13
        assert not any(e.entity_id.endswith("_address_status") for e in entries)

        conn_id = next(e.entity_id for e in entries if e.entity_id.endswith("_connection"))
        conn_state = hass.states.get(conn_id)
        assert conn_state is not None
        assert conn_state.state == "mirror"

        assert await hass.config_entries.async_unload(entry.entry_id)

    @pytest.mark.freeze_time("2026-11-16T07:30:00+00:00")
    async def test_entity_objects_availability_calendar_events_and_diagnostics(
        self,
        hass: HomeAssistant,
        address_entry: MockConfigEntry,
    ) -> None:
        address_entry.add_to_hass(hass)
        coord = UaOutagesCoordinator(hass, address_entry)
        try:
            stab_outage = AddressOutage(
                reason="Стабілізаційне відключення",
                kind=OUTAGE_KIND_STABILIZATION,
                start=datetime(2026, 11, 16, 10, 0, tzinfo=KYIV_TZ),
                end=datetime(2026, 11, 16, 12, 0, tzinfo=KYIV_TZ),
                outage_type="1",
            )
            snap = _snapshot_with_outage(outage=stab_outage)

            with patch(
                "custom_components.ua_outages.coordinator.fetch_dtek_snapshot",
                new=AsyncMock(return_value=snap),
            ):
                data = await coord._async_update_data()
                coord.data = data

            # Instantiate all entity classes directly to inspect localized names & properties.
            sensors = [
                UaOutagesGroupSensor(coord),
                UaOutagesNextOutageSensor(coord),
                UaOutagesOutageEndSensor(coord),
                UaOutagesDailyHoursSensor(coord, "outages_today", is_tomorrow=False),
                UaOutagesDailyHoursSensor(coord, "outages_tomorrow", is_tomorrow=True),
                UaOutagesGroupsStatusSensor(coord),
                UaOutagesAddressStatusSensor(coord),
                UaOutagesRestorationSensor(coord),
                UaOutagesScheduleUpdatedSensor(coord),
                UaOutagesLastUpdateSensor(coord),
                UaOutagesConnectionSensor(coord),
            ]
            assert sensors[0].suggested_object_id == "group"
            assert sensors[0].name == "Черга відключень"
            assert sensors[1].native_value == datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
            assert sensors[2].native_value is None
            assert sensors[3].native_value == 2.0
            assert sensors[4].native_value == 1.0
            assert sensors[5].native_value == "others_all"
            assert sensors[6].available is True
            assert sensors[7].native_value == datetime(2026, 11, 16, 12, 0, tzinfo=KYIV_TZ)
            assert sensors[8].native_value == datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
            assert sensors[9].native_value is not None
            assert sensors[10].native_value == "ok"

            sched_bin = UaOutagesScheduledOutageBinarySensor(coord)
            addr_bin = UaOutagesAddressOutageBinarySensor(coord)
            assert sched_bin.is_on is False
            assert addr_bin.is_on is True
            assert addr_bin.available is True

            # Switch to offline mirror mode: address entities become unavailable.
            with (
                patch(
                    "custom_components.ua_outages.coordinator.fetch_dtek_snapshot",
                    new=AsyncMock(side_effect=DtekBlockedError("waf")),
                ),
                patch(
                    "custom_components.ua_outages.coordinator.fetch_mirror_snapshot",
                    new=AsyncMock(return_value=_snapshot_with_outage(dtek_source=False)),
                ),
            ):
                coord.data = await coord._async_update_data()

            assert sensors[6].available is False
            assert sensors[7].available is False
            assert addr_bin.available is False

            # Calendars include weekly preset and exclude stabilization duplicates.
            main_cal = UaOutagesMainCalendar(coord)
            other_cal = UaOutagesOtherGroupsCalendar(coord)
            events = await main_cal.async_get_events(
                hass,
                datetime(2026, 11, 16, 0, 0, tzinfo=UTC),
                datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
            )
            assert len(events) >= 3
            assert main_cal.event is not None
            other_events = await other_cal.async_get_events(
                hass,
                datetime(2026, 11, 16, 0, 0, tzinfo=UTC),
                datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
            )
            assert any("3.2:" in ev.summary for ev in other_events)

            # EventEntity triggers state update cleanly.
            ev_entity = UaOutagesEventEntity(coord)
            ev_entity.hass = hass
            ev_entity.entity_id = "event.home_events"
            ev_entity.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
            ev_entity._handle_event(EVENT_OUTAGE_STARTED, {"duration_minutes": 120})
            assert ev_entity.state_attributes["event_type"] == EVENT_OUTAGE_STARTED

            # Diagnostics redacts sensitive street/house/title fields.
            address_entry.runtime_data = coord
            diag = await async_get_config_entry_diagnostics(hass, address_entry)
            assert diag["entry"]["title"] == "**REDACTED**"
            assert diag["entry"]["data"]["street"] == "**REDACTED**"
            assert diag["entry"]["data"]["house"] == "**REDACTED**"
            assert diag["entry"]["data"]["site_key"] == "krem"
            assert diag["diagnostics"]["site_key"] == "krem"
            assert STATE_UNAVAILABLE != diag["diagnostics"]["connection"]
        finally:
            await coord.async_shutdown()

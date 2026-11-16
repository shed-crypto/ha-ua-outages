"""Tests for UaOutagesCoordinator, Store persistence, mirrors, backoff, and events."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dtek_client.exceptions import DtekBlockedError, DtekRateLimitError
from dtek_client.stub_client import StubDtekClient
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.ua_outages.api import AddressOutage, ProviderSnapshot, Publication
from custom_components.ua_outages.const import (
    CONF_EXTRA_GROUPS,
    CONF_GROUP,
    CONF_GROUPS_MODE,
    CONF_MANUAL,
    CONF_MODE,
    CONF_SITE_KEY,
    CONF_UPDATE_INTERVAL,
    CONF_USE_MIRROR,
    CONF_WARN_BEFORE_END,
    CONF_WARN_BEFORE_START,
    DOMAIN,
    EVENT_ADDRESS_OUTAGE,
    EVENT_GROUP_CHANGED,
    EVENT_GROUPS_CHANGED,
    EVENT_OUTAGE_END_WARNING,
    EVENT_OUTAGE_ENDED,
    EVENT_OUTAGE_STARTED,
    EVENT_OUTAGE_WARNING,
    EVENT_POSSIBLE_OUTAGE_WARNING,
    EVENT_SCHEDULE_CHANGED,
    EVENT_SCHEDULE_PUBLISHED,
    GROUPS_MODE_ANY,
    MODE_GROUP_ONLY,
    OUTAGE_KIND_EMERGENCY,
    SLOT_MAYBE,
    SLOT_OFF,
    SLOT_ON,
    SLOTS_PER_DAY,
)
from custom_components.ua_outages.coordinator import (
    BUS_EVENT_NAME,
    UaOutagesCoordinator,
    async_remove_storage,
)
from custom_components.ua_outages.schedule import KYIV_TZ


def _make_snapshot(
    *,
    source: str = "dtek",
    fact_update: str = "16.11.2026 12:00",
    fact_updated_at: datetime | None = None,
    today_slots: str | None = None,
    tomorrow_slots: str | None = None,
    groups: list[str] | None = None,
    extra_group_days: dict[str, dict[str, str]] | None = None,
    address_outage: AddressOutage | None = None,
    daily_shown: bool = True,
) -> ProviderSnapshot:
    days_by_group: dict[str, dict[str, str]] = {
        "GPV3.1": {
            "2026-11-16": today_slots or (SLOT_ON * 24 + SLOT_OFF * 4 + SLOT_ON * 20),
        }
    }
    if tomorrow_slots is not None:
        days_by_group["GPV3.1"]["2026-11-17"] = tomorrow_slots
    if extra_group_days:
        for gid, gdays in extra_group_days.items():
            days_by_group[gid] = gdays

    return ProviderSnapshot(
        source=source,
        fact_update=fact_update,
        fact_updated_at=fact_updated_at or datetime(2026, 11, 16, 10, 0, tzinfo=UTC),
        today=datetime(2026, 11, 16, 12, 0, tzinfo=KYIV_TZ).date(),
        days_by_group=days_by_group,
        preset_by_group={"GPV3.1": {1: SLOT_MAYBE * 4 + SLOT_ON * 44}},
        sch_names={"GPV3.1": "Черга 3.1"},
        groups_at_address=groups if groups is not None else ["GPV3.1"],
        external_operator=False,
        address_outage=address_outage,
        publication=(
            Publication(
                daily_shown=daily_shown,
                weekly_shown=True,
                cur_schedule_shown=True,
                cur_outage_shown=True,
            )
            if source == "dtek"
            else None
        ),
    )


class TestCoordinatorFetchAndFallback:
    async def test_initial_dtek_fetch_and_storage_roundtrip(
        self,
        hass: HomeAssistant,
        address_entry: MockConfigEntry,
    ) -> None:
        address_entry.add_to_hass(hass)
        coord = UaOutagesCoordinator(hass, address_entry, client=StubDtekClient("krem"))

        data = await coord._async_update_data()
        coord.async_set_updated_data(data)
        assert data.site_key == "krem"
        assert data.main_group == "GPV3.1"
        assert data.connection == "ok"
        assert data.data_status == "fresh"
        assert data.dtek_reachable is True

        # Persist immediately and reload in a fresh coordinator with offline DTEK.
        await coord._store.async_save(coord._serialize_storage())
        failing_client = MagicMock()
        failing_client.get_home_num = AsyncMock(side_effect=DtekBlockedError("waf"))
        hass.config_entries.async_update_entry(
            address_entry,
            options={CONF_USE_MIRROR: False},
        )
        coord_offline = UaOutagesCoordinator(hass, address_entry, client=failing_client)
        offline_data = await coord_offline._async_update_data()
        assert offline_data.connection == "offline"
        assert offline_data.data_status == "stale"
        assert offline_data.blocked is True
        assert offline_data.consecutive_failures == 1
        assert offline_data.main_group == "GPV3.1"

        coord.async_shutdown_timers()
        coord_offline.async_shutdown_timers()
        await async_remove_storage(hass, address_entry.entry_id)

    async def test_initial_failure_without_cache_raises_update_failed(
        self,
        hass: HomeAssistant,
        address_entry: MockConfigEntry,
    ) -> None:
        address_entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(address_entry, options={CONF_USE_MIRROR: False})
        failing_client = MagicMock()
        failing_client.get_home_num = AsyncMock(side_effect=DtekRateLimitError(retry_after=900))
        coord = UaOutagesCoordinator(hass, address_entry, client=failing_client)

        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        assert coord.update_interval is not None
        assert coord.update_interval.total_seconds() >= 900

    @pytest.mark.freeze_time("2026-11-16T08:00:00+00:00")
    async def test_dtek_failure_falls_back_to_mirror_respecting_freshness(
        self,
        hass: HomeAssistant,
        address_entry: MockConfigEntry,
    ) -> None:
        address_entry.add_to_hass(hass)
        coord = UaOutagesCoordinator(hass, address_entry)

        dtek_snap = _make_snapshot(
            source="dtek",
            fact_updated_at=datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
            today_slots=SLOT_ON * SLOTS_PER_DAY,
        )
        stale_mirror = _make_snapshot(
            source="mirror",
            fact_updated_at=datetime(2026, 11, 16, 11, 0, tzinfo=UTC),
            today_slots=SLOT_OFF * SLOTS_PER_DAY,
        )
        newer_mirror = _make_snapshot(
            source="mirror",
            fact_updated_at=datetime(2026, 11, 16, 13, 0, tzinfo=UTC),
            today_slots=(SLOT_ON * 20) + (SLOT_OFF * 4) + (SLOT_ON * 24),
        )

        with patch(
            "custom_components.ua_outages.coordinator.fetch_dtek_snapshot",
            new=AsyncMock(return_value=dtek_snap),
        ):
            data1 = await coord._async_update_data()
            assert data1.source == "dtek"
            assert data1.today_summary["outage_hours"] == 0.0

        with (
            patch(
                "custom_components.ua_outages.coordinator.fetch_dtek_snapshot",
                new=AsyncMock(side_effect=DtekBlockedError("blocked")),
            ),
            patch(
                "custom_components.ua_outages.coordinator.fetch_mirror_snapshot",
                new=AsyncMock(return_value=stale_mirror),
            ),
        ):
            data2 = await coord._async_update_data()
            assert data2.source == "dtek"
            assert data2.dtek_reachable is False
            assert data2.today_summary["outage_hours"] == 0.0

        with (
            patch(
                "custom_components.ua_outages.coordinator.fetch_dtek_snapshot",
                new=AsyncMock(side_effect=DtekBlockedError("blocked")),
            ),
            patch(
                "custom_components.ua_outages.coordinator.fetch_mirror_snapshot",
                new=AsyncMock(return_value=newer_mirror),
            ),
        ):
            data3 = await coord._async_update_data()
            assert data3.source == "mirror"
            assert data3.connection == "mirror"
            assert data3.today_summary["outage_hours"] == 2.0

        await coord.async_shutdown()

    async def test_dem_group_only_fetches_via_sample_street(
        self,
        hass: HomeAssistant,
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Donetsk Group",
            unique_id="dem|group|GPV3.1",
            data={
                CONF_SITE_KEY: "dem",
                CONF_MODE: MODE_GROUP_ONLY,
                CONF_GROUP: "GPV3.1",
                CONF_MANUAL: True,
            },
        )
        entry.add_to_hass(hass)
        stub = StubDtekClient("dem")
        coord = UaOutagesCoordinator(hass, entry, client=stub)
        data = await coord._async_update_data()
        assert data.site_key == "dem"
        assert data.source == "dtek"
        assert data.main_group == "GPV3.1"
        coord.async_shutdown_timers()


class TestStabilizationDiffAndBoundaryEvents:
    @pytest.mark.freeze_time("2026-11-16T08:00:00+00:00")
    async def test_group_stabilization_and_diff_events(
        self,
        hass: HomeAssistant,
        address_entry: MockConfigEntry,
    ) -> None:
        address_entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(
            address_entry,
            options={
                CONF_GROUPS_MODE: GROUPS_MODE_ANY,
                CONF_UPDATE_INTERVAL: 5,
            },
        )
        coord = UaOutagesCoordinator(hass, address_entry)
        emitted: list[tuple[str, dict[str, Any]]] = []
        unsub = coord.async_add_event_listener(lambda et, p: emitted.append((et, p)))
        bus_events: list[Event] = []
        hass.bus.async_listen(BUS_EVENT_NAME, lambda ev: bus_events.append(ev))

        snap1 = _make_snapshot(
            groups=["GPV3.1", "GPV3.2"],
            extra_group_days={"GPV3.2": {"2026-11-16": SLOT_OFF * 2 + SLOT_ON * 46}},
        )
        snap2 = _make_snapshot(
            groups=["GPV3.2"],
        )
        snap2_only_31 = _make_snapshot(groups=["GPV3.1"])
        snap3 = _make_snapshot(
            groups=["GPV3.1"],
            tomorrow_slots=(SLOT_OFF * 4) + (SLOT_ON * 44),
            address_outage=AddressOutage(
                reason="Аварійні роботи",
                kind=OUTAGE_KIND_EMERGENCY,
                start=datetime(2026, 11, 16, 12, 0, tzinfo=KYIV_TZ),
                end=datetime(2026, 11, 16, 15, 0, tzinfo=KYIV_TZ),
                outage_type="2",
            ),
        )

        with patch(
            "custom_components.ua_outages.coordinator.fetch_dtek_snapshot",
            new=AsyncMock(return_value=snap1),
        ):
            d1 = await coord._async_update_data()
            assert d1.groups_at_address == ["GPV3.1", "GPV3.2"]
            assert d1.extra_groups == ["GPV3.2"]

        for _ in range(2):
            with patch(
                "custom_components.ua_outages.coordinator.fetch_dtek_snapshot",
                new=AsyncMock(return_value=snap2_only_31),
            ):
                d_keep = await coord._async_update_data()
                assert "GPV3.2" in d_keep.groups_at_address

        with patch(
            "custom_components.ua_outages.coordinator.fetch_dtek_snapshot",
            new=AsyncMock(return_value=snap3),
        ):
            d_removed = await coord._async_update_data()
            assert d_removed.groups_at_address == ["GPV3.1"]
            assert d_removed.tomorrow_summary["published"] is True

        for _ in range(3):
            with patch(
                "custom_components.ua_outages.coordinator.fetch_dtek_snapshot",
                new=AsyncMock(return_value=snap2),
            ):
                d_changed = await coord._async_update_data()
        assert d_changed.main_group == "GPV3.2"

        await hass.async_block_till_done()
        event_names = {et for et, _ in emitted}
        assert EVENT_GROUPS_CHANGED in event_names
        assert EVENT_SCHEDULE_PUBLISHED in event_names
        assert EVENT_SCHEDULE_CHANGED in event_names
        assert EVENT_ADDRESS_OUTAGE in event_names
        assert EVENT_GROUP_CHANGED in event_names
        assert len(bus_events) == len(emitted)

        sched = coord.schedule_dict()
        assert sched["site_key"] == "krem"
        assert sched["group_id"] == "GPV3.2"

        unsub()
        await coord.async_shutdown()

    @pytest.mark.freeze_time("2026-11-16T09:00:00+00:00")
    async def test_boundary_timers_fire_warnings_and_transitions(
        self,
        hass: HomeAssistant,
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Test",
            unique_id="krem|group|GPV3.1",
            data={
                CONF_SITE_KEY: "krem",
                CONF_MODE: MODE_GROUP_ONLY,
                CONF_GROUP: "GPV3.1",
                CONF_MANUAL: True,
            },
            options={
                CONF_USE_MIRROR: True,
                CONF_UPDATE_INTERVAL: 60,
                CONF_WARN_BEFORE_START: [30],
                CONF_WARN_BEFORE_END: [15],
                CONF_EXTRA_GROUPS: ["GPV3.2"],
            },
        )
        entry.add_to_hass(hass)
        coord = UaOutagesCoordinator(hass, entry)
        emitted: list[str] = []
        coord.async_add_event_listener(lambda et, _p: emitted.append(et))

        # Day slots: definite outage 12:00-14:00 Kyiv (10:00-12:00 UTC)
        # and possible outage 16:00-17:00 Kyiv (14:00-15:00 UTC).
        day_slots = (
            (SLOT_ON * 24) + (SLOT_OFF * 4) + (SLOT_ON * 4) + (SLOT_MAYBE * 2) + (SLOT_ON * 14)
        )
        snap = _make_snapshot(
            source="mirror",
            today_slots=day_slots,
            extra_group_days={
                "GPV3.2": {"2026-11-16": (SLOT_ON * 24) + (SLOT_OFF * 2) + (SLOT_ON * 22)}
            },
        )

        with patch(
            "custom_components.ua_outages.coordinator.fetch_mirror_snapshot",
            new=AsyncMock(return_value=snap),
        ):
            data = await coord._async_update_data()
            coord.async_set_updated_data(data)

            for tick in (
                datetime(2026, 11, 16, 9, 30, tzinfo=UTC),
                datetime(2026, 11, 16, 10, 0, tzinfo=UTC),
                datetime(2026, 11, 16, 11, 45, tzinfo=UTC),
                datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
                datetime(2026, 11, 16, 13, 30, tzinfo=UTC),
            ):
                async_fire_time_changed(hass, tick)
                await hass.async_block_till_done()

        assert EVENT_OUTAGE_WARNING in emitted
        assert EVENT_OUTAGE_STARTED in emitted
        assert EVENT_OUTAGE_END_WARNING in emitted
        assert EVENT_OUTAGE_ENDED in emitted
        assert EVENT_POSSIBLE_OUTAGE_WARNING in emitted
        await coord.async_shutdown()

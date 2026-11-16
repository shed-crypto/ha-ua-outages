"""Sensor platform for Ukraine Power Outages (11 sensors)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_HOUSE,
    CONF_SETTLEMENT,
    CONF_STREET,
    MODE_ADDRESS,
    OUTAGE_KIND_EMERGENCY,
    OUTAGE_KIND_NONE,
    OUTAGE_KIND_OTHER,
    OUTAGE_KIND_PLANNED,
    OUTAGE_KIND_STABILIZATION,
)
from .coordinator import UaOutagesCoordinator
from .entity import UaOutagesEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ua_outages sensors for a config entry."""
    coordinator: UaOutagesCoordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        UaOutagesGroupSensor(coordinator),
        UaOutagesNextOutageSensor(coordinator),
        UaOutagesOutageEndSensor(coordinator),
        UaOutagesDailyHoursSensor(coordinator, "outages_today", is_tomorrow=False),
        UaOutagesDailyHoursSensor(coordinator, "outages_tomorrow", is_tomorrow=True),
        UaOutagesGroupsStatusSensor(coordinator),
        UaOutagesScheduleUpdatedSensor(coordinator),
        UaOutagesLastUpdateSensor(coordinator),
        UaOutagesConnectionSensor(coordinator),
    ]
    if coordinator.mode == MODE_ADDRESS:
        entities.extend(
            [
                UaOutagesAddressStatusSensor(coordinator),
                UaOutagesRestorationSensor(coordinator),
            ]
        )
    async_add_entities(entities)


class UaOutagesGroupSensor(UaOutagesEntity, SensorEntity):
    """Sensor exposing the primary disconnection group and address metadata."""

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "group")

    @property
    def native_value(self) -> str | None:
        """Return short group code such as '3.1'."""
        gid = self.coordinator.data.main_group
        return gid.removeprefix("GPV") if gid else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return region, group list, mode, and address attributes."""
        data = self.coordinator.data
        entry_data = self.coordinator.config_entry.data
        return {
            "site_key": data.site_key,
            "region": data.region_name,
            "group_id": data.main_group,
            "groups_at_address": data.groups_at_address,
            "extra_groups": data.extra_groups,
            "groups_mode": data.groups_mode,
            "manual": data.manual,
            "settlement": entry_data.get(CONF_SETTLEMENT),
            "street": entry_data.get(CONF_STREET),
            "house": entry_data.get(CONF_HOUSE),
            "external_operator": data.external_operator,
        }


class UaOutagesNextOutageSensor(UaOutagesEntity, SensorEntity):
    """Timestamp sensor for the next upcoming outage start."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "next_outage")

    @property
    def native_value(self) -> datetime | None:
        """Return UTC start timestamp of the next outage interval."""
        iv = self.coordinator.data.next_outage
        return iv.start if iv is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return end timestamp, duration, and freshness metadata."""
        data = self.coordinator.data
        iv = data.next_outage
        return {
            "end": iv.end.isoformat() if iv is not None else None,
            "duration_minutes": iv.duration_minutes if iv is not None else None,
            "data_status": data.data_status,
            "last_verified_at": (
                data.last_verified_at.isoformat() if data.last_verified_at else None
            ),
        }


class UaOutagesOutageEndSensor(UaOutagesEntity, SensorEntity):
    """Timestamp sensor for the end of the currently active outage."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "outage_end")

    @property
    def native_value(self) -> datetime | None:
        """Return UTC end timestamp of the active outage interval."""
        iv = self.coordinator.data.current_outage
        return iv.end if iv is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return start timestamp, duration, and freshness metadata."""
        data = self.coordinator.data
        iv = data.current_outage
        return {
            "start": iv.start.isoformat() if iv is not None else None,
            "duration_minutes": iv.duration_minutes if iv is not None else None,
            "data_status": data.data_status,
            "last_verified_at": (
                data.last_verified_at.isoformat() if data.last_verified_at else None
            ),
        }


class UaOutagesDailyHoursSensor(UaOutagesEntity, SensorEntity):
    """Duration sensor reporting total scheduled outage hours for today or tomorrow."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _unrecorded_attributes = frozenset({"outages", "schedule", "groups"})

    def __init__(
        self,
        coordinator: UaOutagesCoordinator,
        entity_key: str,
        *,
        is_tomorrow: bool,
    ) -> None:
        super().__init__(coordinator, entity_key)
        self._is_tomorrow = is_tomorrow

    def _summary(self) -> dict[str, Any]:
        data = self.coordinator.data
        return data.tomorrow_summary if self._is_tomorrow else data.today_summary

    @property
    def native_value(self) -> float:
        """Return scheduled outage hours for the target day."""
        return float(self._summary().get("outage_hours", 0.0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return textual intervals, raw 48-slot string, per-group schedules, and status."""
        data = self.coordinator.data
        summary = self._summary()
        return {
            "date": summary.get("date"),
            "published": summary.get("published", False),
            "outages_text": summary.get("outages_text", ""),
            "outages": summary.get("outages", []),
            "possible_text": summary.get("possible_text", ""),
            "schedule": summary.get("schedule", ""),
            "groups": summary.get("groups", {}),
            "data_status": data.data_status,
            "last_verified_at": (
                data.last_verified_at.isoformat() if data.last_verified_at else None
            ),
        }


class UaOutagesGroupsStatusSensor(UaOutagesEntity, SensorEntity):
    """Enum sensor summarizing simultaneous state across primary and secondary groups."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["none", "others_some", "others_all", "main", "main_some", "main_all"]
    _unrecorded_attributes = frozenset({"today", "tomorrow", "groups_off"})

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "groups_status")

    @property
    def native_value(self) -> str:
        """Return current multi-group outage status enum."""
        return self.coordinator.data.groups_status_state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return severity level, active off groups, and daily group summaries."""
        data = self.coordinator.data
        return {
            "groups_off": data.groups_off,
            "main_group": data.main_group,
            "extra_groups": data.extra_groups,
            "level": data.groups_status_level,
            "today": data.today_summary.get("groups", {}),
            "tomorrow": data.tomorrow_summary.get("groups", {}),
        }


class UaOutagesAddressStatusSensor(UaOutagesEntity, SensorEntity):
    """Enum sensor reporting DTEK building-level emergency or planned outage status."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        OUTAGE_KIND_NONE,
        OUTAGE_KIND_STABILIZATION,
        OUTAGE_KIND_EMERGENCY,
        OUTAGE_KIND_PLANNED,
        OUTAGE_KIND_OTHER,
    ]

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "address_status")

    @property
    def available(self) -> bool:
        """Mark unavailable when the latest poll could not reach DTEK directly."""
        return super().available and self.coordinator.data.dtek_reachable

    @property
    def native_value(self) -> str:
        """Return building outage kind or 'none'."""
        outage = self.coordinator.data.address_outage
        return outage.kind if outage is not None else OUTAGE_KIND_NONE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return outage reason, timestamps, active flag, and external operator flag."""
        data = self.coordinator.data
        outage = data.address_outage
        return {
            "reason": outage.reason if outage else None,
            "start": outage.start.isoformat() if outage and outage.start else None,
            "end": outage.end.isoformat() if outage and outage.end else None,
            "active": outage is not None,
            "external_operator": data.external_operator,
        }


class UaOutagesRestorationSensor(UaOutagesEntity, SensorEntity):
    """Timestamp sensor reporting expected restoration time for address outages."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "restoration")

    @property
    def available(self) -> bool:
        """Mark unavailable when the latest poll could not reach DTEK directly."""
        return super().available and self.coordinator.data.dtek_reachable

    @property
    def native_value(self) -> datetime | None:
        """Return expected end timestamp when an active address outage reports one."""
        outage = self.coordinator.data.address_outage
        return outage.end if outage is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return outage reason text."""
        outage = self.coordinator.data.address_outage
        return {"reason": outage.reason if outage else None}


class UaOutagesScheduleUpdatedSensor(UaOutagesEntity, SensorEntity):
    """Timestamp sensor reporting when DTEK or mirror last updated the schedule."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "schedule_updated")

    @property
    def native_value(self) -> datetime | None:
        """Return UTC timestamp of the active schedule update."""
        return self.coordinator.data.fact_updated_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return raw version string and active data source."""
        data = self.coordinator.data
        return {
            "version": data.fact_update,
            "source": data.source,
        }


class UaOutagesLastUpdateSensor(UaOutagesEntity, SensorEntity):
    """Diagnostic timestamp sensor for the last successful network poll."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "last_update")

    @property
    def native_value(self) -> datetime | None:
        """Return UTC timestamp of last successful poll."""
        return self.coordinator.data.last_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return latest error string if the last attempt failed."""
        return {"last_error": self.coordinator.data.last_error}


class UaOutagesConnectionSensor(UaOutagesEntity, SensorEntity):
    """Diagnostic enum sensor summarizing connection health and publication flags."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = ["ok", "mirror", "offline"]

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "connection")

    @property
    def native_value(self) -> str:
        """Return 'ok', 'mirror', or 'offline'."""
        return self.coordinator.data.connection

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return detailed connection diagnostics and publication flags."""
        data = self.coordinator.data
        pub = data.publication
        interval = self.coordinator.update_interval
        interval_minutes = round(interval.total_seconds() / 60, 1) if interval else None
        return {
            "last_success": data.last_success.isoformat() if data.last_success else None,
            "last_dtek_success": (
                data.last_dtek_success.isoformat() if data.last_dtek_success else None
            ),
            "consecutive_failures": data.consecutive_failures,
            "update_interval_minutes": interval_minutes,
            "blocked": data.blocked,
            "last_error": data.last_error,
            "daily_schedule_shown": pub.daily_shown if pub else None,
            "weekly_schedule_shown": pub.weekly_shown if pub else None,
        }

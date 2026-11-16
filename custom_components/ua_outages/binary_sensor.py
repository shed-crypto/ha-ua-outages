"""Binary sensor platform for Ukraine Power Outages (2 binary sensors)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MODE_ADDRESS, SLOT_OFF
from .coordinator import UaOutagesCoordinator
from .entity import UaOutagesEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ua_outages binary sensors for a config entry."""
    coordinator: UaOutagesCoordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [UaOutagesScheduledOutageBinarySensor(coordinator)]
    if coordinator.mode == MODE_ADDRESS:
        entities.append(UaOutagesAddressOutageBinarySensor(coordinator))
    async_add_entities(entities)


class UaOutagesScheduledOutageBinarySensor(UaOutagesEntity, BinarySensorEntity):
    """Binary sensor indicating whether a definite scheduled outage is active now."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "scheduled_outage")

    @property
    def is_on(self) -> bool:
        """Return True when the current interval is a confirmed outage."""
        cur = self.coordinator.data.current_outage
        return cur is not None and cur.kind == SLOT_OFF

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return current and next outage bounds alongside freshness metadata."""
        data = self.coordinator.data
        cur = data.current_outage
        nxt = data.next_outage
        return {
            "start": cur.start.isoformat() if cur else None,
            "end": cur.end.isoformat() if cur else None,
            "next_start": nxt.start.isoformat() if nxt else None,
            "next_end": nxt.end.isoformat() if nxt else None,
            "data_status": data.data_status,
            "last_verified_at": (
                data.last_verified_at.isoformat() if data.last_verified_at else None
            ),
        }


class UaOutagesAddressOutageBinarySensor(UaOutagesEntity, BinarySensorEntity):
    """Binary sensor indicating an active building-level emergency or planned outage."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "address_outage")

    @property
    def available(self) -> bool:
        """Mark unavailable when the latest poll could not reach DTEK directly."""
        return super().available and self.coordinator.data.dtek_reachable

    @property
    def is_on(self) -> bool:
        """Return True when DTEK reports an active outage for the configured building."""
        return self.coordinator.data.address_outage is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return outage reason, kind, and bounds."""
        outage = self.coordinator.data.address_outage
        return {
            "reason": outage.reason if outage else None,
            "kind": outage.kind if outage else None,
            "start": outage.start.isoformat() if outage and outage.start else None,
            "end": outage.end.isoformat() if outage and outage.end else None,
        }

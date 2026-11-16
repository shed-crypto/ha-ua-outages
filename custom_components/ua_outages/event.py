"""Event platform for Ukraine Power Outages (10 event types)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import EVENT_TYPES
from .coordinator import UaOutagesCoordinator
from .entity import UaOutagesEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ua_outages EventEntity for a config entry."""
    coordinator: UaOutagesCoordinator = entry.runtime_data
    async_add_entities([UaOutagesEventEntity(coordinator)])


class UaOutagesEventEntity(UaOutagesEntity, EventEntity):
    """Event entity recording outage lifecycle and schedule change events."""

    _attr_event_types = list(EVENT_TYPES)

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "events")

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator event notifications when added to Home Assistant."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_event_listener(self._handle_event))

    @callback
    def _handle_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Record triggered event state and write HA state immediately."""
        if event_type not in EVENT_TYPES:
            return
        attrs = {k: v for k, v in payload.items() if k != "event_type"}
        self._trigger_event(event_type, attrs)
        self.async_write_ha_state()

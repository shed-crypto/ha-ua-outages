"""Service handlers for Ukraine Power Outages."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .coordinator import UaOutagesCoordinator

SERVICE_REFRESH = "refresh"
SERVICE_GET_SCHEDULE = "get_schedule"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
    }
)


def _get_loaded_coordinators(
    hass: HomeAssistant,
    config_entry_id: str | None,
) -> list[UaOutagesCoordinator]:
    """Resolve loaded UaOutagesCoordinator instances for a service call."""
    entries: list[ConfigEntry] = hass.config_entries.async_entries(DOMAIN)
    loaded = [
        entry
        for entry in entries
        if entry.state is ConfigEntryState.LOADED
        and isinstance(getattr(entry, "runtime_data", None), UaOutagesCoordinator)
    ]

    if config_entry_id:
        for entry in loaded:
            if entry.entry_id == config_entry_id:
                return [entry.runtime_data]
        raise ServiceValidationError(f"Config entry {config_entry_id!r} is not loaded")

    if not loaded:
        raise ServiceValidationError("No loaded Ukraine Power Outages entries found")

    return [entry.runtime_data for entry in loaded]


def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration services once on Home Assistant."""
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return

    async def _handle_refresh(call: ServiceCall) -> None:
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        coordinators = _get_loaded_coordinators(hass, entry_id)
        for coordinator in coordinators:
            await coordinator.async_request_refresh()

    async def _handle_get_schedule(call: ServiceCall) -> ServiceResponse:
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        coordinators = _get_loaded_coordinators(hass, entry_id)
        if len(coordinators) == 1:
            result: dict[str, Any] = coordinators[0].schedule_dict()
            return result
        return {
            "entries": [coord.schedule_dict() for coord in coordinators],
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH,
        _handle_refresh,
        schema=SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_SCHEDULE,
        _handle_get_schedule,
        schema=SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove integration services when no entries remain."""
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        hass.services.async_remove(DOMAIN, SERVICE_REFRESH)
    if hass.services.has_service(DOMAIN, SERVICE_GET_SCHEDULE):
        hass.services.async_remove(DOMAIN, SERVICE_GET_SCHEDULE)

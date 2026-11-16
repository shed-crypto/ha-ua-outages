"""The Ukraine Power Outages Home Assistant integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .api import build_user_agent, clear_dtek_clients
from .const import CONF_MODE, DOMAIN, MODE_GROUP_ONLY
from .coordinator import UaOutagesCoordinator, async_remove_storage
from .services import async_setup_services, async_unload_services

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CALENDAR,
    Platform.EVENT,
]

_ADDRESS_ONLY_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("sensor", "address_status"),
    ("sensor", "restoration"),
    ("binary_sensor", "address_outage"),
)

_DATA_STOP_SUB = "stop_listener_registered"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up domain-level services for Ukraine Power Outages."""
    async_setup_services(hass)
    return True


async def _resolve_user_agent(hass: HomeAssistant) -> str:
    """Read integration version asynchronously without blocking the event loop."""
    try:
        integration = await async_get_integration(hass, DOMAIN)
        version = str(integration.version or "0.0.0")
    except Exception:  # noqa: BLE001
        version = "0.0.0"
    return build_user_agent(version)


def _cleanup_stale_address_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove address-only entities when an entry switches to group_only mode."""
    if entry.data.get(CONF_MODE) != MODE_GROUP_ONLY:
        return
    registry = er.async_get(hass)
    for platform, suffix in _ADDRESS_ONLY_SUFFIXES:
        unique_id = f"{entry.entry_id}_{suffix}"
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        if entity_id is not None:
            registry.async_remove(entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ukraine Power Outages from a config entry."""
    async_setup_services(hass)
    _cleanup_stale_address_entities(hass, entry)

    user_agent = await _resolve_user_agent(hass)
    coordinator = UaOutagesCoordinator(hass, entry, user_agent=user_agent)
    entry.runtime_data = coordinator

    await coordinator.async_load_storage()
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(_DATA_STOP_SUB):
        domain_data[_DATA_STOP_SUB] = True

        async def _on_stop(_event: Event) -> None:
            clear_dtek_clients(hass)

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and clean up timers and shared resources when last."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: UaOutagesCoordinator | None = getattr(entry, "runtime_data", None)
    if coordinator is not None:
        await coordinator.async_shutdown()

    if unload_ok:
        remaining = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id and e.state is ConfigEntryState.LOADED
        ]
        if not remaining:
            clear_dtek_clients(hass)
            async_unload_services(hass)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove persisted .storage cache when a config entry is deleted."""
    await async_remove_storage(hass, entry.entry_id)

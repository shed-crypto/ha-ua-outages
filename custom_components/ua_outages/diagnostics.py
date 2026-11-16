"""Diagnostics support for Ukraine Power Outages with address redaction."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOUSE, CONF_STREET
from .coordinator import UaOutagesCoordinator

TO_REDACT: frozenset[str] = frozenset({CONF_STREET, CONF_HOUSE, "title", "entry_title"})


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics payload with street, building, and custom title redacted."""
    coordinator: UaOutagesCoordinator = entry.runtime_data
    data = coordinator.data

    payload: dict[str, Any] = {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "diagnostics": {
            "site_key": data.site_key,
            "region_name": data.region_name,
            "source": data.source,
            "connection": data.connection,
            "data_status": data.data_status,
            "fetch_ok": data.fetch_ok,
            "dtek_reachable": data.dtek_reachable,
            "blocked": data.blocked,
            "consecutive_failures": data.consecutive_failures,
            "last_error": data.last_error,
            "last_success": data.last_success.isoformat() if data.last_success else None,
            "last_dtek_success": (
                data.last_dtek_success.isoformat() if data.last_dtek_success else None
            ),
            "publication": (
                {
                    "daily_shown": data.publication.daily_shown,
                    "weekly_shown": data.publication.weekly_shown,
                    "cur_schedule_shown": data.publication.cur_schedule_shown,
                    "cur_outage_shown": data.publication.cur_outage_shown,
                }
                if data.publication
                else None
            ),
        },
        "schedule": coordinator.schedule_dict(),
    }

    return dict(async_redact_data(payload, TO_REDACT))

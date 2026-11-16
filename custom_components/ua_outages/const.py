"""Constants for the Ukraine Power Outages Home Assistant integration."""

from __future__ import annotations

from typing import Final

from dtek_client.const import (
    DEFAULT_TIMEZONE,
    DTEK_SITES,
    DTEK_WEEKDAY,
    REGION_NAMES,
    REGION_NAMES_EN,
    REGION_NAMES_UA,
)

__all__ = [
    "ADDRESS_OUTAGE_EVENT_KINDS",
    "CONF_EXCLUDED_GROUPS",
    "CONF_EXTRA_GROUPS",
    "CONF_GROUP",
    "CONF_GROUPS_MODE",
    "CONF_HOUSE",
    "CONF_LANGUAGE",
    "CONF_MANUAL",
    "CONF_MODE",
    "CONF_NAME",
    "CONF_SETTLEMENT",
    "CONF_SHOW_POSSIBLE",
    "CONF_SITE_KEY",
    "CONF_STREET",
    "CONF_UPDATE_INTERVAL",
    "CONF_USE_MIRROR",
    "CONF_WARN_BEFORE_END",
    "CONF_WARN_BEFORE_START",
    "DATA_CLIENTS",
    "DEFAULT_GROUPS",
    "DEFAULT_GROUPS_MODE",
    "DEFAULT_LANGUAGE",
    "DEFAULT_SHOW_POSSIBLE",
    "DEFAULT_TIMEZONE",
    "DEFAULT_UPDATE_INTERVAL",
    "DEFAULT_USE_MIRROR",
    "DEFAULT_WARN_BEFORE_END",
    "DEFAULT_WARN_BEFORE_START",
    "DOMAIN",
    "DTEK_SITES",
    "DTEK_WEEKDAY",
    "EVENT_ADDRESS_OUTAGE",
    "EVENT_GROUP_CHANGED",
    "EVENT_GROUPS_CHANGED",
    "EVENT_OUTAGE_END_WARNING",
    "EVENT_OUTAGE_ENDED",
    "EVENT_OUTAGE_STARTED",
    "EVENT_OUTAGE_WARNING",
    "EVENT_POSSIBLE_OUTAGE_WARNING",
    "EVENT_SCHEDULE_CHANGED",
    "EVENT_SCHEDULE_PUBLISHED",
    "EVENT_TYPES",
    "FACT_UPDATE_FORMAT",
    "GROUP_REMOVAL_UPDATES",
    "GROUPS_MODE_ALL",
    "GROUPS_MODE_ANY",
    "GROUPS_MODE_MAIN",
    "GROUPS_MODES",
    "HISTORY_DAYS",
    "HOUSE_DATE_FORMATS",
    "LANGUAGES",
    "MAX_UPDATE_INTERVAL",
    "MIN_UPDATE_INTERVAL",
    "MIRROR_TIMEOUT_SECONDS",
    "MIRROR_URLS",
    "MODE_ADDRESS",
    "MODE_GROUP_ONLY",
    "OUTAGE_KIND_EMERGENCY",
    "OUTAGE_KIND_NONE",
    "OUTAGE_KIND_OTHER",
    "OUTAGE_KIND_PLANNED",
    "OUTAGE_KIND_STABILIZATION",
    "POSSIBLE_DAYS_AHEAD",
    "REGION_NAMES",
    "REGION_NAMES_EN",
    "REGION_NAMES_UA",
    "RETRY_JITTER",
    "RETRY_MAX_INTERVAL",
    "SLOT_MAYBE",
    "SLOT_MINUTES",
    "SLOT_OFF",
    "SLOT_ON",
    "SLOTS_PER_DAY",
    "STORAGE_SAVE_DELAY",
    "STORAGE_VERSION",
    "WARN_MINUTE_OPTIONS",
]

DOMAIN: Final = "ua_outages"
DATA_CLIENTS: Final = "clients"

CONF_SITE_KEY: Final = "site_key"
CONF_MODE: Final = "mode"
CONF_SETTLEMENT: Final = "settlement"
CONF_STREET: Final = "street"
CONF_HOUSE: Final = "house"
CONF_GROUP: Final = "group"
CONF_MANUAL: Final = "manual"
CONF_NAME: Final = "name"

MODE_ADDRESS: Final = "address"
MODE_GROUP_ONLY: Final = "group_only"

CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_WARN_BEFORE_START: Final = "warn_before_start"
CONF_WARN_BEFORE_END: Final = "warn_before_end"
CONF_USE_MIRROR: Final = "use_mirror"
CONF_SHOW_POSSIBLE: Final = "show_possible"
CONF_GROUPS_MODE: Final = "groups_mode"
CONF_EXCLUDED_GROUPS: Final = "excluded_groups"
CONF_EXTRA_GROUPS: Final = "extra_groups"
CONF_LANGUAGE: Final = "language"

GROUPS_MODE_MAIN: Final = "main"
GROUPS_MODE_ANY: Final = "any"
GROUPS_MODE_ALL: Final = "all"
GROUPS_MODES: Final[tuple[str, ...]] = (
    GROUPS_MODE_MAIN,
    GROUPS_MODE_ANY,
    GROUPS_MODE_ALL,
)

LANGUAGES: Final[tuple[str, ...]] = ("uk", "en", "ru")
DEFAULT_LANGUAGE: Final = "uk"

DEFAULT_UPDATE_INTERVAL: Final = 5
MIN_UPDATE_INTERVAL: Final = 2
MAX_UPDATE_INTERVAL: Final = 60
RETRY_MAX_INTERVAL: Final = 15
RETRY_JITTER: Final = 0.1

DEFAULT_WARN_BEFORE_START: Final[list[int]] = [30, 15]
DEFAULT_WARN_BEFORE_END: Final[list[int]] = [15]
WARN_MINUTE_OPTIONS: Final[tuple[int, ...]] = (1, 5, 10, 15, 30, 60, 120)

DEFAULT_USE_MIRROR: Final = True
DEFAULT_SHOW_POSSIBLE: Final = True
DEFAULT_GROUPS_MODE: Final = GROUPS_MODE_MAIN

GROUP_REMOVAL_UPDATES: Final = 3
STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 30

SLOTS_PER_DAY: Final = 48
SLOT_MINUTES: Final = 30
SLOT_ON: Final = "1"
SLOT_OFF: Final = "0"
SLOT_MAYBE: Final = "?"

HISTORY_DAYS: Final = 7
POSSIBLE_DAYS_AHEAD: Final = 7

HOUSE_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%H:%M %d.%m.%Y",
    "%d.%m.%Y %H:%M",
)
FACT_UPDATE_FORMAT: Final = "%d.%m.%Y %H:%M"

MIRROR_TIMEOUT_SECONDS: Final = 15
MIRROR_URLS: Final[dict[str, str | None]] = {
    "kem": "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv.json",
    "krem": "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json",
    "dnem": "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/dnipro.json",
    "oem": "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/odesa.json",
    "dem": None,
}

# Fallback group identifiers used when DTEK returns no preset group list.
DEFAULT_GROUPS: Final[tuple[str, ...]] = (
    "GPV1.1",
    "GPV1.2",
    "GPV2.1",
    "GPV2.2",
    "GPV3.1",
    "GPV3.2",
    "GPV4.1",
    "GPV4.2",
    "GPV5.1",
    "GPV5.2",
    "GPV6.1",
    "GPV6.2",
)

OUTAGE_KIND_NONE: Final = "none"
OUTAGE_KIND_STABILIZATION: Final = "stabilization"
OUTAGE_KIND_EMERGENCY: Final = "emergency"
OUTAGE_KIND_PLANNED: Final = "planned"
OUTAGE_KIND_OTHER: Final = "other"

ADDRESS_OUTAGE_EVENT_KINDS: Final[tuple[str, ...]] = (
    OUTAGE_KIND_EMERGENCY,
    OUTAGE_KIND_PLANNED,
    OUTAGE_KIND_OTHER,
)

EVENT_SCHEDULE_PUBLISHED: Final = "schedule_published"
EVENT_SCHEDULE_CHANGED: Final = "schedule_changed"
EVENT_OUTAGE_WARNING: Final = "outage_warning"
EVENT_POSSIBLE_OUTAGE_WARNING: Final = "possible_outage_warning"
EVENT_OUTAGE_END_WARNING: Final = "outage_end_warning"
EVENT_OUTAGE_STARTED: Final = "outage_started"
EVENT_OUTAGE_ENDED: Final = "outage_ended"
EVENT_ADDRESS_OUTAGE: Final = "address_outage"
EVENT_GROUP_CHANGED: Final = "group_changed"
EVENT_GROUPS_CHANGED: Final = "groups_changed"

EVENT_TYPES: Final[tuple[str, ...]] = (
    EVENT_SCHEDULE_PUBLISHED,
    EVENT_SCHEDULE_CHANGED,
    EVENT_OUTAGE_WARNING,
    EVENT_POSSIBLE_OUTAGE_WARNING,
    EVENT_OUTAGE_END_WARNING,
    EVENT_OUTAGE_STARTED,
    EVENT_OUTAGE_ENDED,
    EVENT_ADDRESS_OUTAGE,
    EVENT_GROUP_CHANGED,
    EVENT_GROUPS_CHANGED,
)

"""Provider adapter over dtek-blackout-client and Baskerville42 mirror fallback."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from dtek_client import DtekClient
from dtek_client.exceptions import DtekClientError
from dtek_client.models import FactSchedule, HomeNumResponse, HouseEntry, PresetSchedule
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession

from .const import (
    DATA_CLIENTS,
    DEFAULT_TIMEZONE,
    DOMAIN,
    FACT_UPDATE_FORMAT,
    HOUSE_DATE_FORMATS,
    MIRROR_TIMEOUT_SECONDS,
    MIRROR_URLS,
    OUTAGE_KIND_EMERGENCY,
    OUTAGE_KIND_NONE,
    OUTAGE_KIND_OTHER,
    OUTAGE_KIND_PLANNED,
    OUTAGE_KIND_STABILIZATION,
)
from .schedule import KYIV_TZ, parse_hours

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AddressOutage:
    """Normalized active outage banner for a specific building."""

    reason: str
    kind: str
    start: datetime | None
    end: datetime | None
    outage_type: str

    def to_dict(self) -> dict[str, object]:
        """Serialize outage details for storage and diagnostics."""
        return {
            "reason": self.reason,
            "kind": self.kind,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "type": self.outage_type,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AddressOutage:
        """Restore AddressOutage from persisted storage dictionary."""
        start_raw = data.get("start")
        end_raw = data.get("end")
        return cls(
            reason=str(data.get("reason") or ""),
            kind=str(data.get("kind") or OUTAGE_KIND_NONE),
            start=datetime.fromisoformat(start_raw) if isinstance(start_raw, str) else None,
            end=datetime.fromisoformat(end_raw) if isinstance(end_raw, str) else None,
            outage_type=str(data.get("type") or ""),
        )


@dataclass(frozen=True, slots=True)
class Publication:
    """Schedule publication visibility flags returned by DTEK."""

    daily_shown: bool
    weekly_shown: bool
    cur_schedule_shown: bool
    cur_outage_shown: bool


@dataclass(frozen=True, slots=True)
class ProviderSnapshot:
    """Unified normalized schedule snapshot from DTEK or a GitHub mirror."""

    source: str
    fact_update: str | None
    fact_updated_at: datetime | None
    today: date | None
    days_by_group: dict[str, dict[str, str]]
    preset_by_group: dict[str, dict[int, str]]
    sch_names: dict[str, str]
    groups_at_address: list[str]
    external_operator: bool
    address_outage: AddressOutage | None
    publication: Publication | None


def build_user_agent(version: str) -> str:
    """Build User-Agent string identifying this integration version."""
    clean_version = version.strip() or "0.0.0"
    return f"ha-ua-outages/{clean_version} HomeAssistant"


def get_dtek_client(
    hass: HomeAssistant,
    site_key: str,
    *,
    user_agent: str | None = None,
) -> DtekClient:
    """Return a shared DtekClient instance per site_key using HA's session factory."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    clients: dict[str, DtekClient] = domain_data.setdefault(DATA_CLIENTS, {})
    if site_key not in clients:
        clients[site_key] = DtekClient(
            site_key=site_key,
            session_factory=lambda: async_create_clientsession(
                hass,
                cookie_jar=aiohttp.CookieJar(),
            ),
            user_agent=user_agent,
        )
    return clients[site_key]


def clear_dtek_clients(hass: HomeAssistant) -> None:
    """Drop cached DtekClient references from hass.data when no entries remain."""
    domain_data = hass.data.get(DOMAIN)
    if isinstance(domain_data, dict):
        domain_data.pop(DATA_CLIENTS, None)


def normalize_group_ids(group_ids: Iterable[str]) -> list[str]:
    """Deduplicate and normalize group identifiers while preserving order."""
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in group_ids:
        gid = raw.strip()
        if gid and gid not in seen:
            seen.add(gid)
            normalized.append(gid)
    return normalized


def classify_outage_kind(sub_type: str) -> str:
    """Classify Ukrainian DTEK outage reason text into normalized enum values."""
    text = sub_type.strip().casefold()
    if not text:
        return OUTAGE_KIND_NONE
    if "стабіліз" in text:
        return OUTAGE_KIND_STABILIZATION
    if "аварій" in text or "екстрен" in text:
        return OUTAGE_KIND_EMERGENCY
    if "план" in text:
        return OUTAGE_KIND_PLANNED
    return OUTAGE_KIND_OTHER


def parse_house_datetime(raw: str | None, tz: ZoneInfo = KYIV_TZ) -> datetime | None:
    """Parse DTEK house outage timestamp supporting both '%H:%M %d.%m.%Y' and '%d.%m.%Y %H:%M'."""
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    for fmt in HOUSE_DATE_FORMATS:
        try:
            naive = datetime.strptime(cleaned, fmt)
            return naive.replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def parse_update_timestamp(raw: str | None, tz: ZoneInfo = KYIV_TZ) -> datetime | None:
    """Parse DTEK '%d.%m.%Y %H:%M' or ISO-8601 timestamps into aware UTC datetimes."""
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        naive = datetime.strptime(cleaned, FACT_UPDATE_FORMAT)
        return naive.replace(tzinfo=tz).astimezone(UTC)
    except ValueError:
        pass
    try:
        iso_dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if iso_dt.tzinfo is None:
            return iso_dt.replace(tzinfo=tz).astimezone(UTC)
        return iso_dt.astimezone(UTC)
    except ValueError:
        return None


def house_entry_to_outage(entry: HouseEntry | None) -> AddressOutage | None:
    """Convert a DtekClient HouseEntry into an AddressOutage when an outage is reported."""
    if entry is None or not entry.has_current_outage:
        return None
    start = parse_house_datetime(entry.start_date)
    end = parse_house_datetime(entry.end_date)
    if start is not None and end is not None and end < start:
        end = None
    reason = entry.sub_type.strip()
    return AddressOutage(
        reason=reason,
        kind=classify_outage_kind(reason),
        start=start,
        end=end,
        outage_type=entry.outage_type.strip(),
    )


def _convert_fact(
    fact: FactSchedule | None,
    time_zone: Mapping[str, str] | None,
) -> dict[str, dict[str, str]]:
    """Convert a FactSchedule model into {group_id: {iso_date: 48-char slots}}."""
    if fact is None:
        return {}
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    days_by_group: dict[str, dict[str, str]] = {}

    for ts_str, group_map in fact.days.items():
        try:
            day_iso = datetime.fromtimestamp(int(ts_str), tz=tz).date().isoformat()
        except (ValueError, OSError, OverflowError):
            continue
        for raw_gid, slot_map in group_map.items():
            gid = raw_gid.strip()
            if not gid:
                continue
            encoded = parse_hours(slot_map, time_zone=time_zone)
            if encoded is not None:
                days_by_group.setdefault(gid, {})[day_iso] = encoded

    return days_by_group


def _convert_preset(preset: PresetSchedule | None) -> dict[str, dict[int, str]]:
    """Convert a PresetSchedule model into {group_id: {weekday_1..7: 48-char slots}}."""
    if preset is None:
        return {}
    tz_map = preset.time_zone or None
    preset_by_group: dict[str, dict[int, str]] = {}

    for raw_gid, group_week in preset.groups.items():
        gid = raw_gid.strip()
        if not gid:
            continue
        weekdays: dict[int, str] = {}
        for weekday_idx, day_schedule in group_week.days.items():
            encoded = parse_hours(day_schedule.slots, time_zone=tz_map)
            if encoded is not None:
                weekdays[weekday_idx] = encoded
        if weekdays:
            preset_by_group[gid] = weekdays

    return preset_by_group


def snapshot_from_home_num(
    response: HomeNumResponse,
    *,
    house_number: str | None = None,
) -> ProviderSnapshot:
    """Convert a DtekClient HomeNumResponse into a normalized ProviderSnapshot."""
    time_zone = response.preset.time_zone if response.preset else None
    days_by_group = _convert_fact(response.fact, time_zone)
    preset_by_group = _convert_preset(response.preset)
    sch_names = dict(response.preset.sch_names) if response.preset else {}

    fact_update = response.fact.update if response.fact else None
    fact_updated_at = parse_update_timestamp(fact_update)
    if fact_updated_at is None:
        fact_updated_at = parse_update_timestamp(response.update_timestamp)

    today: date | None = None
    if response.fact and response.fact.today_ts > 0:
        today = response.fact.today_date

    house_entry = response.find_house(house_number) if house_number else None
    groups_at_address = normalize_group_ids(house_entry.group_ids) if house_entry else []
    external_operator = bool(house_entry.external_operator) if house_entry else False
    address_outage = house_entry_to_outage(house_entry)

    publication = Publication(
        daily_shown=response.show_table_fact and response.show_table_schedule,
        weekly_shown=response.show_table_plan,
        cur_schedule_shown=response.show_cur_schedule,
        cur_outage_shown=response.show_cur_outage_param,
    )

    return ProviderSnapshot(
        source="dtek",
        fact_update=fact_update,
        fact_updated_at=fact_updated_at,
        today=today,
        days_by_group=days_by_group,
        preset_by_group=preset_by_group,
        sch_names=sch_names,
        groups_at_address=groups_at_address,
        external_operator=external_operator,
        address_outage=address_outage,
        publication=publication,
    )


def snapshot_from_mirror_payload(raw: Mapping[str, Any]) -> ProviderSnapshot:
    """Parse a Baskerville42/outage-data-ua JSON payload using dtek_client models."""
    raw_fact = raw.get("fact")
    raw_preset = raw.get("preset")

    fact: FactSchedule | None = None
    if isinstance(raw_fact, Mapping):
        fact_dict = dict(raw_fact)
        if not fact_dict.get("update") and isinstance(fact_dict.get("updateFact"), str):
            fact_dict["update"] = fact_dict["updateFact"]
        fact = FactSchedule.model_validate(fact_dict)

    preset: PresetSchedule | None = None
    if isinstance(raw_preset, Mapping):
        preset = PresetSchedule.model_validate(dict(raw_preset))

    time_zone = preset.time_zone if preset else None
    days_by_group = _convert_fact(fact, time_zone)
    preset_by_group = _convert_preset(preset)
    sch_names = dict(preset.sch_names) if preset else {}

    last_updated_raw = raw.get("lastUpdated")
    fact_update = (
        fact.update
        if fact and fact.update
        else (str(last_updated_raw) if isinstance(last_updated_raw, str) else None)
    )
    fact_updated_at = parse_update_timestamp(fact.update if fact else None)
    if fact_updated_at is None and isinstance(last_updated_raw, str):
        fact_updated_at = parse_update_timestamp(last_updated_raw)

    today: date | None = None
    if fact and fact.today_ts > 0:
        today = fact.today_date

    return ProviderSnapshot(
        source="mirror",
        fact_update=fact_update,
        fact_updated_at=fact_updated_at,
        today=today,
        days_by_group=days_by_group,
        preset_by_group=preset_by_group,
        sch_names=sch_names,
        groups_at_address=[],
        external_operator=False,
        address_outage=None,
        publication=None,
    )


async def fetch_dtek_snapshot(
    client: DtekClient,
    city: str,
    street: str,
    house_number: str | None = None,
) -> ProviderSnapshot:
    """Fetch live DTEK schedule and address outage snapshot using FORCE_UPDATE_FACT."""
    # Always rely on DtekClient's default FORCE_UPDATE_FACT so DTEK returns fresh
    # showTable* flags alongside fact and preset in a single request.
    response = await client.get_home_num(city, street)
    return snapshot_from_home_num(response, house_number=house_number)


async def fetch_mirror_snapshot(
    hass: HomeAssistant,
    site_key: str,
) -> ProviderSnapshot | None:
    """Fetch schedule snapshot from the regional GitHub mirror, or None if unsupported."""
    url = MIRROR_URLS.get(site_key)
    if not url:
        return None

    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=MIRROR_TIMEOUT_SECONDS)
    async with session.get(url, timeout=timeout) as resp:
        resp.raise_for_status()
        payload = await resp.json(content_type=None)

    if not isinstance(payload, Mapping):
        raise ValueError(f"Unexpected mirror payload type for {site_key}: {type(payload)}")

    return snapshot_from_mirror_payload(payload)


def is_dtek_client_error(exc: Exception) -> bool:
    """Return True when exc belongs to the DtekClientError hierarchy."""
    return isinstance(exc, DtekClientError)

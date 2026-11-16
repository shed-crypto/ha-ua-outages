"""DataUpdateCoordinator, persistent storage, boundary timers, and event dispatcher."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from dtek_client import DtekClient
from dtek_client.exceptions import DtekBlockedError, DtekClientError, DtekRateLimitError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AddressOutage,
    ProviderSnapshot,
    Publication,
    fetch_dtek_snapshot,
    fetch_mirror_snapshot,
    get_dtek_client,
)
from .const import (
    ADDRESS_OUTAGE_EVENT_KINDS,
    CONF_EXCLUDED_GROUPS,
    CONF_EXTRA_GROUPS,
    CONF_GROUP,
    CONF_GROUPS_MODE,
    CONF_HOUSE,
    CONF_MANUAL,
    CONF_MODE,
    CONF_SETTLEMENT,
    CONF_SITE_KEY,
    CONF_STREET,
    CONF_UPDATE_INTERVAL,
    CONF_USE_MIRROR,
    CONF_WARN_BEFORE_END,
    CONF_WARN_BEFORE_START,
    DEFAULT_GROUPS_MODE,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_USE_MIRROR,
    DEFAULT_WARN_BEFORE_END,
    DEFAULT_WARN_BEFORE_START,
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
    GROUP_REMOVAL_UPDATES,
    HISTORY_DAYS,
    MIRROR_URLS,
    MODE_ADDRESS,
    MODE_GROUP_ONLY,
    REGION_NAMES_UA,
    RETRY_JITTER,
    RETRY_MAX_INTERVAL,
    SLOT_OFF,
    SLOTS_PER_DAY,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)
from .schedule import (
    KYIV_TZ,
    Interval,
    build_intervals,
    combine_slots,
    current_slot,
    future_changes,
    merge_days,
    summarize_day,
)

_LOGGER = logging.getLogger(__name__)
BUS_EVENT_NAME = f"{DOMAIN}_event"


@dataclass(frozen=True, slots=True)
class CoordinatorData:
    """Immutable coordinator snapshot consumed by all integration platforms."""

    site_key: str
    region_name: str
    mode: str
    main_group: str
    groups_at_address: list[str]
    extra_groups: list[str]
    groups_mode: str
    manual: bool
    external_operator: bool
    effective_days: dict[str, str]
    group_days: dict[str, dict[str, str]]
    group_presets: dict[str, dict[int, str]]
    sch_names: dict[str, str]
    intervals: list[Interval]
    today_date: date
    tomorrow_date: date
    today_summary: dict[str, Any]
    tomorrow_summary: dict[str, Any]
    current_outage: Interval | None
    next_outage: Interval | None
    address_outage: AddressOutage | None
    address_history: list[AddressOutage]
    publication: Publication | None
    fact_update: str | None
    fact_updated_at: datetime | None
    source: str
    fetch_ok: bool
    dtek_reachable: bool
    data_status: str
    connection: str
    blocked: bool
    consecutive_failures: int
    last_error: str | None
    last_success: datetime | None
    last_dtek_success: datetime | None
    last_verified_at: datetime | None
    groups_status_state: str
    groups_status_level: int
    groups_off: list[str]
    group_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)


def _storage_key(entry_id: str) -> str:
    """Return the Home Assistant Store key for one config entry."""
    return f"{DOMAIN}.{entry_id}"


async def async_remove_storage(hass: HomeAssistant, entry_id: str) -> None:
    """Delete persisted storage file when a config entry is removed."""
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, _storage_key(entry_id))
    await store.async_remove()


def _parse_iso_dt(raw: Any) -> datetime | None:
    """Parse an ISO-8601 string safely into an aware UTC datetime."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _outage_key(outage: AddressOutage | None) -> tuple[str, str, str | None, str | None] | None:
    """Build a comparable identity key for an AddressOutage banner."""
    if outage is None:
        return None
    return (
        outage.kind,
        outage.reason,
        outage.start.isoformat() if outage.start else None,
        outage.end.isoformat() if outage.end else None,
    )


def _compute_groups_status(
    main_group: str,
    extra_groups: list[str],
    group_days: Mapping[str, Mapping[str, str]],
    today_iso: str,
    cur_slot: int,
) -> tuple[str, int, list[str]]:
    """Compute groups_status enum state, severity level (0..5), and currently off groups."""

    def _is_group_off(gid: str) -> bool:
        slots = group_days.get(gid, {}).get(today_iso)
        if not slots or len(slots) != SLOTS_PER_DAY or not (0 <= cur_slot < SLOTS_PER_DAY):
            return False
        return slots[cur_slot] == SLOT_OFF

    main_off = _is_group_off(main_group)
    others_off = [gid for gid in extra_groups if _is_group_off(gid)]
    groups_off = ([main_group] if main_off else []) + others_off

    if not extra_groups:
        return ("main", 3, groups_off) if main_off else ("none", 0, groups_off)

    all_others_off = len(others_off) == len(extra_groups)
    some_others_off = len(others_off) > 0

    if main_off:
        if all_others_off:
            return ("main_all", 5, groups_off)
        if some_others_off:
            return ("main_some", 4, groups_off)
        return ("main", 3, groups_off)

    if all_others_off:
        return ("others_all", 2, groups_off)
    if some_others_off:
        return ("others_some", 1, groups_off)
    return ("none", 0, groups_off)


class UaOutagesCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Coordinator managing DTEK polling, mirrors, .storage cache, timers, and events."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        *,
        client: DtekClient | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.config_entry = config_entry
        self._user_agent = user_agent
        self._injected_client = client
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION,
            _storage_key(config_entry.entry_id),
        )
        self._storage_loaded = False
        self._boundary_unsub: CALLBACK_TYPE | None = None
        self._event_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._sample_address: tuple[str, str] | None = None

        # Persisted operational state
        self._group_days: dict[str, dict[str, str]] = {}
        self._group_presets: dict[str, dict[int, str]] = {}
        self._sch_names: dict[str, str] = {}
        self._fact_update: str | None = None
        self._fact_updated_at: datetime | None = None
        self._publication: Publication | None = None
        self._groups_at_address: list[str] = []
        self._group_misses: dict[str, int] = {}
        self._external_operator = False
        self._address_outage: AddressOutage | None = None
        self._address_history: list[AddressOutage] = []
        self._last_success: datetime | None = None
        self._last_dtek_success: datetime | None = None
        self._last_verified_at: datetime | None = None
        self._source: str = "storage"
        self._main_group: str = str(config_entry.data.get(CONF_GROUP) or "")

        # Ephemeral connection diagnostics & timer state
        self._fetch_ok = False
        self._dtek_reachable = False
        self._blocked = False
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._fired_boundary_keys: set[tuple[str, str, int]] = set()
        self._last_timer_check: datetime | None = None
        self._has_initial_data = False

        base_minutes = self._base_interval_minutes()
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{config_entry.entry_id}",
            update_interval=timedelta(minutes=base_minutes),
        )

    @property
    def site_key(self) -> str:
        """Configured DTEK regional key."""
        return str(self.config_entry.data[CONF_SITE_KEY])

    @property
    def mode(self) -> str:
        """Configured entry mode ('address' or 'group_only')."""
        return str(self.config_entry.data.get(CONF_MODE, MODE_ADDRESS))

    @property
    def is_manual(self) -> bool:
        """Return True if the primary group is manually locked by user configuration."""
        return bool(self.config_entry.data.get(CONF_MANUAL, self.mode == MODE_GROUP_ONLY))

    def _base_interval_minutes(self) -> int:
        raw = self.config_entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        return max(1, int(raw))

    def _warn_before_start(self) -> list[int]:
        raw = self.config_entry.options.get(CONF_WARN_BEFORE_START, DEFAULT_WARN_BEFORE_START)
        return sorted({int(x) for x in raw if int(x) > 0}, reverse=True)

    def _warn_before_end(self) -> list[int]:
        raw = self.config_entry.options.get(CONF_WARN_BEFORE_END, DEFAULT_WARN_BEFORE_END)
        return sorted({int(x) for x in raw if int(x) > 0}, reverse=True)

    def _use_mirror(self) -> bool:
        return bool(self.config_entry.options.get(CONF_USE_MIRROR, DEFAULT_USE_MIRROR))

    def _groups_mode(self) -> str:
        return str(self.config_entry.options.get(CONF_GROUPS_MODE, DEFAULT_GROUPS_MODE))

    def _extra_groups(self) -> list[str]:
        if self.mode == MODE_GROUP_ONLY:
            configured = self.config_entry.options.get(CONF_EXTRA_GROUPS, [])
            return [str(g) for g in configured if str(g) and str(g) != self._main_group]

        excluded = {str(g) for g in self.config_entry.options.get(CONF_EXCLUDED_GROUPS, [])}
        return [
            gid
            for gid in self._groups_at_address
            if gid != self._main_group and gid not in excluded
        ]

    @callback
    def async_add_event_listener(
        self,
        listener: Callable[[str, dict[str, Any]], None],
    ) -> Callable[[], None]:
        """Register a callback invoked whenever an integration event fires."""
        self._event_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._event_listeners:
                self._event_listeners.remove(listener)

        return _remove

    def _emit_event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Dispatch an event both to registered EventEntity listeners and HA bus."""
        data = {
            "entry_id": self.config_entry.entry_id,
            "config_entry_title": self.config_entry.title,
            "event_type": event_type,
            **(payload or {}),
        }
        self.hass.bus.async_fire(BUS_EVENT_NAME, data)
        for listener in list(self._event_listeners):
            listener(event_type, data)

    def _get_client(self) -> DtekClient:
        if self._injected_client is not None:
            return self._injected_client
        return get_dtek_client(self.hass, self.site_key, user_agent=self._user_agent)

    async def async_load_storage(self) -> None:
        """Load persisted schedule state from .storage if not already loaded."""
        if self._storage_loaded:
            return
        self._storage_loaded = True
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return

        raw_days = stored.get("group_days")
        if isinstance(raw_days, dict):
            self._group_days = {
                str(gid): {str(d): str(s) for d, s in day_map.items()}
                for gid, day_map in raw_days.items()
                if isinstance(day_map, dict)
            }

        raw_presets = stored.get("group_presets")
        if isinstance(raw_presets, dict):
            self._group_presets = {
                str(gid): {int(wd): str(s) for wd, s in wd_map.items()}
                for gid, wd_map in raw_presets.items()
                if isinstance(wd_map, dict)
            }

        raw_names = stored.get("sch_names")
        if isinstance(raw_names, dict):
            self._sch_names = {str(k): str(v) for k, v in raw_names.items()}

        if isinstance(stored.get("fact_update"), str):
            self._fact_update = stored["fact_update"]
        self._fact_updated_at = _parse_iso_dt(stored.get("fact_updated_at"))

        raw_pub = stored.get("publication")
        if isinstance(raw_pub, dict):
            self._publication = Publication(
                daily_shown=bool(raw_pub.get("daily_shown", True)),
                weekly_shown=bool(raw_pub.get("weekly_shown", True)),
                cur_schedule_shown=bool(raw_pub.get("cur_schedule_shown", True)),
                cur_outage_shown=bool(raw_pub.get("cur_outage_shown", True)),
            )

        raw_groups = stored.get("groups_at_address")
        if isinstance(raw_groups, list):
            self._groups_at_address = [str(g) for g in raw_groups if g]
        raw_misses = stored.get("group_misses")
        if isinstance(raw_misses, dict):
            self._group_misses = {str(k): int(v) for k, v in raw_misses.items()}
        self._external_operator = bool(stored.get("external_operator", False))

        raw_outage = stored.get("address_outage")
        if isinstance(raw_outage, dict):
            self._address_outage = AddressOutage.from_dict(raw_outage)

        raw_history = stored.get("address_history")
        if isinstance(raw_history, list):
            self._address_history = [
                AddressOutage.from_dict(item) for item in raw_history if isinstance(item, dict)
            ]

        self._last_success = _parse_iso_dt(stored.get("last_success"))
        self._last_dtek_success = _parse_iso_dt(stored.get("last_dtek_success"))
        self._last_verified_at = self._last_success
        if isinstance(stored.get("source"), str):
            self._source = stored["source"]
        if isinstance(stored.get("main_group"), str) and stored["main_group"]:
            self._main_group = stored["main_group"]

        if self._group_days or self._main_group:
            self._has_initial_data = True
            self.data = self._build_data(dt_util.utcnow())
            self.async_update_listeners()

    def _serialize_storage(self) -> dict[str, Any]:
        """Serialize current state for delayed .storage persistence."""
        return {
            "group_days": self._group_days,
            "group_presets": {
                gid: {str(wd): slots for wd, slots in wd_map.items()}
                for gid, wd_map in self._group_presets.items()
            },
            "sch_names": self._sch_names,
            "fact_update": self._fact_update,
            "fact_updated_at": (
                self._fact_updated_at.isoformat() if self._fact_updated_at else None
            ),
            "publication": (
                {
                    "daily_shown": self._publication.daily_shown,
                    "weekly_shown": self._publication.weekly_shown,
                    "cur_schedule_shown": self._publication.cur_schedule_shown,
                    "cur_outage_shown": self._publication.cur_outage_shown,
                }
                if self._publication is not None
                else None
            ),
            "groups_at_address": self._groups_at_address,
            "group_misses": self._group_misses,
            "external_operator": self._external_operator,
            "address_outage": self._address_outage.to_dict() if self._address_outage else None,
            "address_history": [item.to_dict() for item in self._address_history],
            "last_success": self._last_success.isoformat() if self._last_success else None,
            "last_dtek_success": (
                self._last_dtek_success.isoformat() if self._last_dtek_success else None
            ),
            "source": self._source,
            "main_group": self._main_group,
        }

    def _schedule_storage_save(self) -> None:
        self._store.async_delay_save(self._serialize_storage, STORAGE_SAVE_DELAY)

    async def _fetch_snapshot(self) -> ProviderSnapshot:
        """Fetch fresh snapshot from DTEK or mirror according to mode and fallback rules."""
        mirror_url = MIRROR_URLS.get(self.site_key)
        client = self._get_client()

        if self.mode == MODE_ADDRESS:
            city = str(self.config_entry.data[CONF_SETTLEMENT])
            street = str(self.config_entry.data[CONF_STREET])
            house = str(self.config_entry.data[CONF_HOUSE])
            try:
                return await fetch_dtek_snapshot(client, city, street, house)
            except DtekClientError as exc:
                if self._use_mirror() and mirror_url:
                    _LOGGER.warning(
                        "Direct DTEK fetch failed for %s (%s); trying mirror fallback",
                        self.site_key,
                        exc,
                    )
                    mirror_snap = await fetch_mirror_snapshot(self.hass, self.site_key)
                    if mirror_snap is not None:
                        return mirror_snap
                raise

        if self._use_mirror() and mirror_url:
            mirror_snap = await fetch_mirror_snapshot(self.hass, self.site_key)
            if mirror_snap is not None:
                return mirror_snap

        if self._sample_address is None:
            all_streets = await client.get_all_streets()
            for sample_city, street_list in all_streets.items():
                if street_list:
                    self._sample_address = (sample_city, street_list[0])
                    break
        if self._sample_address is None:
            raise UpdateFailed(f"No streets available to fetch schedule for {self.site_key}")

        return await fetch_dtek_snapshot(
            client,
            self._sample_address[0],
            self._sample_address[1],
            None,
        )

    def _apply_stabilized_address_groups(self, snapshot: ProviderSnapshot) -> None:
        """Update groups_at_address using GROUP_REMOVAL_UPDATES stabilization."""
        incoming = list(snapshot.groups_at_address)
        old_groups = list(self._groups_at_address)

        if not self._groups_at_address:
            self._groups_at_address = incoming
            self._group_misses.clear()
        else:
            incoming_set = set(incoming)
            stabilized: list[str] = []
            for gid in incoming:
                self._group_misses.pop(gid, None)
                stabilized.append(gid)
            for gid in self._groups_at_address:
                if gid in incoming_set:
                    continue
                misses = self._group_misses.get(gid, 0) + 1
                if misses < GROUP_REMOVAL_UPDATES:
                    self._group_misses[gid] = misses
                    stabilized.append(gid)
                else:
                    self._group_misses.pop(gid, None)
            self._groups_at_address = stabilized

        if old_groups and self._groups_at_address != old_groups:
            self._emit_event(
                EVENT_GROUPS_CHANGED,
                {"old_groups": old_groups, "new_groups": list(self._groups_at_address)},
            )

        if not self.is_manual and self._groups_at_address:
            new_primary = self._groups_at_address[0]
            if self._main_group and new_primary != self._main_group:
                old_primary = self._main_group
                self._main_group = new_primary
                self._emit_event(
                    EVENT_GROUP_CHANGED,
                    {"old_group": old_primary, "new_group": new_primary},
                )
            elif not self._main_group:
                self._main_group = new_primary

    def _update_address_history(self, new_outage: AddressOutage | None, now_utc: datetime) -> None:
        """Track historical non-stabilization address outages within HISTORY_DAYS."""
        cutoff = now_utc - timedelta(days=HISTORY_DAYS)
        self._address_history = [
            item for item in self._address_history if (item.end or item.start or now_utc) >= cutoff
        ]
        if new_outage is None or new_outage.kind not in ADDRESS_OUTAGE_EVENT_KINDS:
            return
        new_key = _outage_key(new_outage)
        for existing in self._address_history:
            if _outage_key(existing) == new_key:
                return
        self._address_history.append(new_outage)

    def _should_accept_mirror(self, snapshot: ProviderSnapshot) -> bool:
        """Accept mirror data only if its timestamp is newer than stored data."""
        if not self._has_initial_data or self._fact_updated_at is None:
            return True
        if snapshot.fact_updated_at is None:
            return False
        return snapshot.fact_updated_at > self._fact_updated_at

    def _apply_snapshot(self, snapshot: ProviderSnapshot, now_utc: datetime) -> None:
        """Merge a fetched ProviderSnapshot into coordinator state and emit diff events."""
        local_now = now_utc.astimezone(KYIV_TZ)
        today = local_now.date()
        cur_slot = current_slot(local_now, KYIV_TZ)

        old_effective = self._compute_effective_days()
        had_initial = self._has_initial_data

        if snapshot.source == "dtek":
            self._dtek_reachable = True
            self._last_dtek_success = now_utc
            self._publication = snapshot.publication

            if self.mode == MODE_ADDRESS:
                self._external_operator = snapshot.external_operator
                self._apply_stabilized_address_groups(snapshot)
                old_outage_key = _outage_key(self._address_outage)
                new_outage_key = _outage_key(snapshot.address_outage)
                self._address_outage = snapshot.address_outage
                self._update_address_history(snapshot.address_outage, now_utc)
                if new_outage_key is not None and new_outage_key != old_outage_key:
                    assert snapshot.address_outage is not None
                    self._emit_event(EVENT_ADDRESS_OUTAGE, snapshot.address_outage.to_dict())
        else:
            self._dtek_reachable = False

        accept_schedule = snapshot.source == "dtek" or self._should_accept_mirror(snapshot)
        clear_future = bool(
            snapshot.source == "dtek"
            and snapshot.publication is not None
            and not snapshot.publication.daily_shown
        )

        if accept_schedule:
            tracked_groups = (
                set(self._group_days.keys())
                | set(snapshot.days_by_group.keys())
                | {self._main_group}
                | set(self._extra_groups())
            )
            for gid in tracked_groups:
                if not gid:
                    continue
                old_grp = self._group_days.get(gid, {})
                new_grp = snapshot.days_by_group.get(gid, {})
                self._group_days[gid] = merge_days(
                    old_grp,
                    new_grp,
                    today,
                    cur_slot,
                    clear_future_today=clear_future,
                )
            for gid, preset_map in snapshot.preset_by_group.items():
                self._group_presets[gid] = dict(preset_map)
            if snapshot.sch_names:
                self._sch_names.update(snapshot.sch_names)

            self._fact_update = snapshot.fact_update
            self._fact_updated_at = snapshot.fact_updated_at
            self._source = snapshot.source

        self._last_success = now_utc
        self._last_verified_at = now_utc
        self._fetch_ok = True
        self._blocked = False
        self._consecutive_failures = 0
        self._last_error = None
        self._has_initial_data = True

        new_effective = self._compute_effective_days()
        if had_initial:
            today_iso = today.isoformat()
            new_future_dates = sorted(
                d for d in new_effective if d > today_iso and d not in old_effective
            )
            for day_str in new_future_dates:
                self._emit_event(
                    EVENT_SCHEDULE_PUBLISHED,
                    {"date": day_str, "schedule": new_effective[day_str]},
                )

            if future_changes(old_effective, new_effective, today, cur_slot):
                self._emit_event(
                    EVENT_SCHEDULE_CHANGED,
                    {
                        "previous": {k: v for k, v in old_effective.items() if k >= today_iso},
                        "current": {k: v for k, v in new_effective.items() if k >= today_iso},
                    },
                )

    def _compute_effective_days(self) -> dict[str, str]:
        """Combine main and extra groups across all stored ISO dates."""
        primary_days = self._group_days.get(self._main_group, {})
        extras = self._extra_groups()
        mode = self._groups_mode()
        if mode == DEFAULT_GROUPS_MODE or not extras:
            return dict(primary_days)

        combined: dict[str, str] = {}
        for day_str, primary_slots in primary_days.items():
            other_slots = [
                self._group_days[gid][day_str]
                for gid in extras
                if day_str in self._group_days.get(gid, {})
            ]
            combined[day_str] = combine_slots(primary_slots, other_slots, mode)
        return combined

    def _build_data(self, now_utc: datetime) -> CoordinatorData:
        """Compute full CoordinatorData view for now_utc."""
        local_now = now_utc.astimezone(KYIV_TZ)
        today = local_now.date()
        tomorrow = today + timedelta(days=1)
        today_iso = today.isoformat()
        tomorrow_iso = tomorrow.isoformat()
        cur_slot = current_slot(local_now, KYIV_TZ)

        effective_days = self._compute_effective_days()
        intervals = build_intervals(effective_days, KYIV_TZ)

        current_iv: Interval | None = None
        next_iv: Interval | None = None
        for iv in intervals:
            if iv.contains(now_utc):
                current_iv = iv
            elif iv.start > now_utc and next_iv is None:
                next_iv = iv

        extras = self._extra_groups()
        groups_mode = self._groups_mode()

        def _summary_for(day_obj: date, day_iso: str) -> dict[str, Any]:
            slots = effective_days.get(day_iso)
            if slots is None:
                return {
                    "date": day_iso,
                    "published": False,
                    "outage_hours": 0.0,
                    "possible_hours": 0.0,
                    "outages_text": "",
                    "possible_text": "",
                    "outages": [],
                    "schedule": "",
                    "groups": {},
                }
            summary = summarize_day(day_obj, slots, KYIV_TZ)
            summary["published"] = True
            summary["groups"] = {
                gid: self._group_days.get(gid, {}).get(day_iso, "")
                for gid in [self._main_group, *extras]
                if gid
            }
            return summary

        status_state, status_level, groups_off = _compute_groups_status(
            self._main_group,
            extras,
            self._group_days,
            today_iso,
            cur_slot,
        )

        if not self._fetch_ok:
            connection = "offline"
            data_status = "stale"
        elif self._source == "mirror":
            connection = "mirror"
            data_status = "mirror"
        else:
            connection = "ok"
            data_status = "fresh"

        region_name = REGION_NAMES_UA.get(self.site_key, self.site_key)

        return CoordinatorData(
            site_key=self.site_key,
            region_name=region_name,
            mode=self.mode,
            main_group=self._main_group,
            groups_at_address=list(self._groups_at_address),
            extra_groups=extras,
            groups_mode=groups_mode,
            manual=self.is_manual,
            external_operator=self._external_operator,
            effective_days=effective_days,
            group_days={gid: dict(d) for gid, d in self._group_days.items()},
            group_presets={gid: dict(p) for gid, p in self._group_presets.items()},
            sch_names=dict(self._sch_names),
            intervals=intervals,
            today_date=today,
            tomorrow_date=tomorrow,
            today_summary=_summary_for(today, today_iso),
            tomorrow_summary=_summary_for(tomorrow, tomorrow_iso),
            current_outage=current_iv,
            next_outage=next_iv,
            address_outage=self._address_outage,
            address_history=list(self._address_history),
            publication=self._publication,
            fact_update=self._fact_update,
            fact_updated_at=self._fact_updated_at,
            source=self._source,
            fetch_ok=self._fetch_ok,
            dtek_reachable=self._dtek_reachable,
            data_status=data_status,
            connection=connection,
            blocked=self._blocked,
            consecutive_failures=self._consecutive_failures,
            last_error=self._last_error,
            last_success=self._last_success,
            last_dtek_success=self._last_dtek_success,
            last_verified_at=self._last_verified_at,
            groups_status_state=status_state,
            groups_status_level=status_level,
            groups_off=groups_off,
        )

    def _compute_backoff_interval(self, exc: Exception) -> timedelta:
        """Calculate exponential backoff interval with +/-10% jitter."""
        base = self._base_interval_minutes()
        max_minutes = max(RETRY_MAX_INTERVAL, base)
        exp_minutes = min(max_minutes, base * (2 ** max(0, self._consecutive_failures - 1)))
        jitter = 1.0 + random.uniform(-RETRY_JITTER, RETRY_JITTER)  # noqa: S311
        delay_seconds = exp_minutes * 60.0 * jitter
        if isinstance(exc, DtekRateLimitError) and exc.retry_after is not None:
            delay_seconds = max(delay_seconds, float(exc.retry_after))
        return timedelta(seconds=max(30.0, delay_seconds))

    async def _async_update_data(self) -> CoordinatorData:
        """Fetch latest data from DTEK/mirror or serve cached storage data on failure."""
        await self.async_load_storage()
        now_utc = dt_util.utcnow()

        try:
            snapshot = await self._fetch_snapshot()
        except Exception as exc:  # noqa: BLE001
            self._consecutive_failures += 1
            self._fetch_ok = False
            self._dtek_reachable = False
            self._blocked = isinstance(exc, DtekBlockedError)
            self._last_error = str(exc)
            self.update_interval = self._compute_backoff_interval(exc)

            if self._has_initial_data:
                _LOGGER.warning(
                    "Outage update failed (%s); serving cached data (%d consecutive failures)",
                    exc,
                    self._consecutive_failures,
                )
                data = self._build_data(now_utc)
                self._schedule_boundary_timer(now_utc, data)
                return data

            raise UpdateFailed(f"Failed to fetch initial outage schedule: {exc}") from exc

        self._apply_snapshot(snapshot, now_utc)
        self.update_interval = timedelta(minutes=self._base_interval_minutes())
        self._schedule_storage_save()

        data = self._build_data(now_utc)
        self._check_boundary_events(now_utc, data)
        self._schedule_boundary_timer(now_utc, data)
        return data

    def _next_boundary_utc(self, now_utc: datetime, data: CoordinatorData) -> datetime:
        """Find the earliest future boundary UTC timestamp across intervals and midnight."""
        local_now = now_utc.astimezone(KYIV_TZ)
        next_midnight = datetime.combine(
            local_now.date() + timedelta(days=1),
            time(0, 0),
            tzinfo=KYIV_TZ,
        ).astimezone(UTC)

        candidates: list[datetime] = [next_midnight]
        warn_starts = self._warn_before_start()
        warn_ends = self._warn_before_end()

        for iv in data.intervals:
            if iv.start > now_utc:
                candidates.append(iv.start)
            if iv.end > now_utc:
                candidates.append(iv.end)
            for mins in warn_starts:
                warn_at = iv.start - timedelta(minutes=mins)
                if warn_at > now_utc:
                    candidates.append(warn_at)
            if iv.kind == SLOT_OFF:
                for mins in warn_ends:
                    warn_at = iv.end - timedelta(minutes=mins)
                    if warn_at > now_utc:
                        candidates.append(warn_at)

        if data.address_outage is not None:
            if data.address_outage.start and data.address_outage.start.astimezone(UTC) > now_utc:
                candidates.append(data.address_outage.start.astimezone(UTC))
            if data.address_outage.end and data.address_outage.end.astimezone(UTC) > now_utc:
                candidates.append(data.address_outage.end.astimezone(UTC))

        return min(candidates)

    def _check_boundary_events(self, now_utc: datetime, data: CoordinatorData) -> None:
        """Fire transition and warning events crossed between _last_timer_check and now_utc."""
        prev_check = self._last_timer_check
        self._last_timer_check = now_utc
        if prev_check is None:
            return

        # Prune stale dedup keys older than 48h to bound memory.
        if len(self._fired_boundary_keys) > 256:
            cutoff_iso = (now_utc - timedelta(hours=48)).isoformat()
            self._fired_boundary_keys = {
                item for item in self._fired_boundary_keys if item[1] >= cutoff_iso
            }

        warn_starts = self._warn_before_start()
        warn_ends = self._warn_before_end()

        for iv in data.intervals:
            if iv.kind == SLOT_OFF and prev_check < iv.start <= now_utc:
                key = (EVENT_OUTAGE_STARTED, iv.start.isoformat(), 0)
                if key not in self._fired_boundary_keys:
                    self._fired_boundary_keys.add(key)
                    self._emit_event(EVENT_OUTAGE_STARTED, iv.to_dict(KYIV_TZ))

            if iv.kind == SLOT_OFF and prev_check < iv.end <= now_utc:
                key = (EVENT_OUTAGE_ENDED, iv.end.isoformat(), 0)
                if key not in self._fired_boundary_keys:
                    self._fired_boundary_keys.add(key)
                    self._emit_event(EVENT_OUTAGE_ENDED, iv.to_dict(KYIV_TZ))

            for mins in warn_starts:
                warn_at = iv.start - timedelta(minutes=mins)
                if prev_check < warn_at <= now_utc:
                    ev_type = (
                        EVENT_OUTAGE_WARNING
                        if iv.kind == SLOT_OFF
                        else EVENT_POSSIBLE_OUTAGE_WARNING
                    )
                    key = (ev_type, iv.start.isoformat(), mins)
                    if key not in self._fired_boundary_keys:
                        self._fired_boundary_keys.add(key)
                        self._emit_event(ev_type, {"minutes": mins, **iv.to_dict(KYIV_TZ)})

            if iv.kind == SLOT_OFF:
                for mins in warn_ends:
                    warn_at = iv.end - timedelta(minutes=mins)
                    if prev_check < warn_at <= now_utc:
                        key = (EVENT_OUTAGE_END_WARNING, iv.end.isoformat(), mins)
                        if key not in self._fired_boundary_keys:
                            self._fired_boundary_keys.add(key)
                            self._emit_event(
                                EVENT_OUTAGE_END_WARNING,
                                {"minutes": mins, **iv.to_dict(KYIV_TZ)},
                            )

    def _schedule_boundary_timer(self, now_utc: datetime, data: CoordinatorData) -> None:
        """Schedule the next local state recalculation at the earliest boundary tick."""
        if self._boundary_unsub is not None:
            self._boundary_unsub()
            self._boundary_unsub = None

        next_tick = self._next_boundary_utc(now_utc, data)
        self._boundary_unsub = async_track_point_in_utc_time(
            self.hass,
            self._handle_boundary_timer,
            next_tick,
        )

    @callback
    def _handle_boundary_timer(self, now_utc: datetime) -> None:
        """Handle exact slot boundary, warning threshold, or midnight without network calls."""
        self._boundary_unsub = None
        data = self._build_data(now_utc)
        self._check_boundary_events(now_utc, data)
        # Update coordinator data and notify entities without resetting the network polling timer.
        self.data = data
        self.async_update_listeners()
        self._schedule_boundary_timer(now_utc, data)

    def async_shutdown_timers(self) -> None:
        """Cancel scheduled boundary timer on entry unload."""
        if self._boundary_unsub is not None:
            self._boundary_unsub()
            self._boundary_unsub = None

    async def async_shutdown(self) -> None:
        """Cancel boundary and coordinator timers on shutdown or unload."""
        self.async_shutdown_timers()
        await super().async_shutdown()

    def schedule_dict(self) -> dict[str, Any]:
        """Return structured schedule payload for ua_outages.get_schedule and diagnostics."""
        data = self.data or self._build_data(dt_util.utcnow())
        return {
            "entry_id": self.config_entry.entry_id,
            "title": self.config_entry.title,
            "site_key": data.site_key,
            "region": data.region_name,
            "mode": data.mode,
            "group_id": data.main_group,
            "groups_at_address": data.groups_at_address,
            "extra_groups": data.extra_groups,
            "groups_mode": data.groups_mode,
            "source": data.source,
            "connection": data.connection,
            "data_status": data.data_status,
            "fact_update": data.fact_update,
            "today": data.today_summary,
            "tomorrow": data.tomorrow_summary,
            "days": {
                day_str: summarize_day(date.fromisoformat(day_str), slots, KYIV_TZ)
                for day_str, slots in sorted(data.effective_days.items())
            },
            "address_outage": data.address_outage.to_dict() if data.address_outage else None,
        }

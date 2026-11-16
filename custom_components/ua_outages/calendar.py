"""Calendar platform for Ukraine Power Outages (2 calendars)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import AddressOutage
from .const import (
    CONF_SHOW_POSSIBLE,
    DEFAULT_LANGUAGE,
    DEFAULT_SHOW_POSSIBLE,
    OUTAGE_KIND_EMERGENCY,
    OUTAGE_KIND_PLANNED,
    OUTAGE_KIND_STABILIZATION,
    POSSIBLE_DAYS_AHEAD,
    SLOT_OFF,
)
from .coordinator import CoordinatorData, UaOutagesCoordinator
from .entity import CALENDAR_LABELS, UaOutagesEntity
from .schedule import KYIV_TZ, build_intervals, possible_days


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ua_outages calendar entities."""
    coordinator: UaOutagesCoordinator = entry.runtime_data
    async_add_entities(
        [
            UaOutagesMainCalendar(coordinator),
            UaOutagesOtherGroupsCalendar(coordinator),
        ]
    )


def _overlaps(
    ev_start: datetime,
    ev_end: datetime,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """Return True if [ev_start, ev_end) overlaps [window_start, window_end)."""
    return ev_end > window_start and ev_start < window_end


def _address_outage_to_event(
    outage: AddressOutage,
    labels: dict[str, str],
) -> CalendarEvent | None:
    """Convert a non-stabilization AddressOutage into a CalendarEvent."""
    if outage.kind == OUTAGE_KIND_STABILIZATION or outage.start is None:
        return None
    end = outage.end or (outage.start + timedelta(hours=1))
    if outage.kind == OUTAGE_KIND_EMERGENCY:
        summary = labels["emergency"]
    elif outage.kind == OUTAGE_KIND_PLANNED:
        summary = labels["planned"]
    else:
        summary = labels["other"]
    return CalendarEvent(
        start=outage.start.astimezone(KYIV_TZ),
        end=end.astimezone(KYIV_TZ),
        summary=summary,
        description=outage.reason or None,
    )


class _BaseOutagesCalendar(UaOutagesEntity, CalendarEntity):
    """Shared helper logic for outage calendars."""

    def _labels(self) -> dict[str, str]:
        return CALENDAR_LABELS.get(self.language, CALENDAR_LABELS[DEFAULT_LANGUAGE])

    def _show_possible(self, data: CoordinatorData) -> bool:
        enabled = bool(
            self.coordinator.config_entry.options.get(CONF_SHOW_POSSIBLE, DEFAULT_SHOW_POSSIBLE)
        )
        weekly_allowed = data.publication is None or data.publication.weekly_shown
        return enabled and weekly_allowed

    def _all_events(self) -> list[CalendarEvent]:
        raise NotImplementedError

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next upcoming CalendarEvent."""
        now_utc = dt_util.utcnow()
        events = sorted(self._all_events(), key=lambda ev: ev.start)
        for ev in events:
            ev_end = ev.end if isinstance(ev.end, datetime) else datetime.min.replace(tzinfo=UTC)
            if ev_end.astimezone(UTC) > now_utc:
                return ev
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return all calendar events overlapping [start_date, end_date)."""
        win_start = (
            start_date.astimezone(UTC) if start_date.tzinfo else start_date.replace(tzinfo=UTC)
        )
        win_end = end_date.astimezone(UTC) if end_date.tzinfo else end_date.replace(tzinfo=UTC)
        result: list[CalendarEvent] = []
        for ev in self._all_events():
            if not isinstance(ev.start, datetime) or not isinstance(ev.end, datetime):
                continue
            if _overlaps(ev.start.astimezone(UTC), ev.end.astimezone(UTC), win_start, win_end):
                result.append(ev)
        result.sort(key=lambda ev: ev.start)
        return result


class UaOutagesMainCalendar(_BaseOutagesCalendar):
    """Primary calendar showing confirmed, possible, weekly preset, and address outages."""

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "outages")

    def _all_events(self) -> list[CalendarEvent]:
        data = self.coordinator.data
        labels = self._labels()
        merged_days = dict(data.effective_days)

        if self._show_possible(data):
            preset_map = data.group_presets.get(data.main_group, {})
            existing = {date.fromisoformat(d) for d in merged_days}
            forecast = possible_days(
                preset_map,
                data.today_date,
                days_ahead=POSSIBLE_DAYS_AHEAD,
                existing_days=existing,
            )
            merged_days.update(forecast)

        events: list[CalendarEvent] = []
        for iv in build_intervals(merged_days, KYIV_TZ):
            summary = (
                labels["definite_outage"] if iv.kind == SLOT_OFF else labels["possible_outage"]
            )
            events.append(
                CalendarEvent(
                    start=iv.start.astimezone(KYIV_TZ),
                    end=iv.end.astimezone(KYIV_TZ),
                    summary=summary,
                )
            )

        seen_keys: set[tuple[datetime, datetime, str]] = set()
        address_items = list(data.address_history)
        if data.address_outage is not None:
            address_items.append(data.address_outage)

        for outage in address_items:
            cal_ev = _address_outage_to_event(outage, labels)
            if (
                cal_ev is None
                or not isinstance(cal_ev.start, datetime)
                or not isinstance(cal_ev.end, datetime)
            ):
                continue
            dedup = (cal_ev.start, cal_ev.end, cal_ev.summary)
            if dedup not in seen_keys:
                seen_keys.add(dedup)
                events.append(cal_ev)

        return events


class UaOutagesOtherGroupsCalendar(_BaseOutagesCalendar):
    """Secondary calendar displaying outage intervals for other tracked groups."""

    def __init__(self, coordinator: UaOutagesCoordinator) -> None:
        super().__init__(coordinator, "other_groups")

    def _all_events(self) -> list[CalendarEvent]:
        data = self.coordinator.data
        labels = self._labels()
        events: list[CalendarEvent] = []
        show_possible = self._show_possible(data)

        for gid in data.extra_groups:
            group_days = dict(data.group_days.get(gid, {}))
            if show_possible:
                preset_map = data.group_presets.get(gid, {})
                existing = {date.fromisoformat(d) for d in group_days}
                group_days.update(
                    possible_days(
                        preset_map,
                        data.today_date,
                        days_ahead=POSSIBLE_DAYS_AHEAD,
                        existing_days=existing,
                    )
                )
            short_gid = gid.removeprefix("GPV")
            for iv in build_intervals(group_days, KYIV_TZ):
                base_label = (
                    labels["definite_outage"] if iv.kind == SLOT_OFF else labels["possible_outage"]
                )
                events.append(
                    CalendarEvent(
                        start=iv.start.astimezone(KYIV_TZ),
                        end=iv.end.astimezone(KYIV_TZ),
                        summary=f"{labels['group_prefix']} {short_gid}: {base_label}",
                    )
                )

        return events

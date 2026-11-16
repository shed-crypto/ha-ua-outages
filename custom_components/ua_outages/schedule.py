"""Schedule math, 48-slot representation, and DST-safe UTC interval builders."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dtek_client.models import SlotStatus

from .const import (
    DEFAULT_TIMEZONE,
    GROUPS_MODE_ALL,
    GROUPS_MODE_ANY,
    GROUPS_MODE_MAIN,
    HISTORY_DAYS,
    POSSIBLE_DAYS_AHEAD,
    SLOT_MAYBE,
    SLOT_MINUTES,
    SLOT_OFF,
    SLOT_ON,
    SLOTS_PER_DAY,
)

KYIV_TZ = ZoneInfo(DEFAULT_TIMEZONE)

_HOUR_SLOT_MAP: dict[SlotStatus, str] = {
    SlotStatus.YES: "11",
    SlotStatus.NO: "00",
    SlotStatus.FIRST: "01",
    SlotStatus.SECOND: "10",
    SlotStatus.MAYBE: "??",
    SlotStatus.MFIRST: "?1",
    SlotStatus.MSECOND: "1?",
    SlotStatus.UNKNOWN: "??",
}

_HALF_HOUR_SLOT_MAP: dict[SlotStatus, str] = {
    SlotStatus.YES: SLOT_ON,
    SlotStatus.NO: SLOT_OFF,
    SlotStatus.MAYBE: SLOT_MAYBE,
    SlotStatus.FIRST: SLOT_MAYBE,
    SlotStatus.SECOND: SLOT_MAYBE,
    SlotStatus.MFIRST: SLOT_MAYBE,
    SlotStatus.MSECOND: SLOT_MAYBE,
    SlotStatus.UNKNOWN: SLOT_MAYBE,
}

_OUTAGE_CHARS: frozenset[str] = frozenset({SLOT_OFF, SLOT_MAYBE})


@dataclass(frozen=True, slots=True)
class Interval:
    """Contiguous UTC time interval of a single outage state."""

    start: datetime
    end: datetime
    kind: str

    @property
    def duration_minutes(self) -> int:
        """Return interval duration rounded to whole minutes."""
        return int((self.end - self.start).total_seconds() // 60)

    def contains(self, moment: datetime) -> bool:
        """Return True when moment falls inside [start, end)."""
        moment_utc = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
        return self.start <= moment_utc < self.end

    def format_local(self, tz: ZoneInfo = KYIV_TZ) -> str:
        """Format interval bounds in local HH:MM-HH:MM notation."""
        local_start = self.start.astimezone(tz)
        local_end = self.end.astimezone(tz)
        end_str = (
            "24:00"
            if (
                local_end.hour == 0
                and local_end.minute == 0
                and local_end.date() > local_start.date()
            )
            else local_end.strftime("%H:%M")
        )
        return f"{local_start.strftime('%H:%M')}-{end_str}"

    def to_dict(self, tz: ZoneInfo = KYIV_TZ) -> dict[str, object]:
        """Serialize interval for sensor attributes and service responses."""
        return {
            "start": self.start.astimezone(tz).isoformat(),
            "end": self.end.astimezone(tz).isoformat(),
            "kind": "outage" if self.kind == SLOT_OFF else "possible",
            "duration_minutes": self.duration_minutes,
        }


def _coerce_slot_status(raw: SlotStatus | str | None) -> SlotStatus:
    """Normalize string or SlotStatus values case-insensitively."""
    if isinstance(raw, SlotStatus):
        return raw
    if isinstance(raw, str):
        return SlotStatus(raw.strip().lower())
    return SlotStatus.UNKNOWN


def _is_half_hour_grid(
    slots: Mapping[str, SlotStatus | str],
    time_zone: Mapping[str, str] | None,
) -> bool:
    """Determine whether a slot mapping uses 48 half-hour keys or 24 hourly keys."""
    if time_zone is not None and len(time_zone) == SLOTS_PER_DAY:
        return True
    for key in slots:
        try:
            if int(key) > 24:
                return True
        except (ValueError, TypeError):
            continue
    return False


def parse_hours(
    slots: Mapping[str, SlotStatus | str],
    *,
    time_zone: Mapping[str, str] | None = None,
) -> str | None:
    """Convert 24-hour or 48-half-hour DTEK slot mappings into a 48-char day string."""
    if not slots:
        return None

    if _is_half_hour_grid(slots, time_zone):
        parts = [
            _HALF_HOUR_SLOT_MAP[_coerce_slot_status(slots.get(str(idx)))]
            for idx in range(1, SLOTS_PER_DAY + 1)
        ]
        return "".join(parts)

    parts = [_HOUR_SLOT_MAP[_coerce_slot_status(slots.get(str(idx)))] for idx in range(1, 25)]
    return "".join(parts)


def current_slot(now: datetime, tz: ZoneInfo = KYIV_TZ) -> int:
    """Return the 0-based 30-minute slot index (0..47) in tz for a datetime."""
    local_dt = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    return local_dt.hour * 2 + local_dt.minute // SLOT_MINUTES


def _local_exists(local_naive: datetime, tz: ZoneInfo) -> bool:
    """Return True if local_naive exists in tz (False inside spring DST gap)."""
    aware = local_naive.replace(tzinfo=tz, fold=0)
    roundtrip = aware.astimezone(UTC).astimezone(tz)
    return roundtrip.replace(tzinfo=None) == local_naive


def _is_ambiguous_hour(local_naive: datetime, tz: ZoneInfo) -> bool:
    """Return True if local_naive occurs twice in tz (autumn DST fold)."""
    utc_0 = local_naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    utc_1 = local_naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    return utc_0 != utc_1


def _day_utc_slices(day: date, slots: str, tz: ZoneInfo) -> list[tuple[datetime, datetime, str]]:
    """Expand a 48-char day string into ordered 30-minute UTC slices.

    Spring-forward non-existent local slots are skipped, while autumn fall-back
    ambiguous slots are emitted for both fold=0 and fold=1 passes so the 25-hour
    transition day stays contiguous in UTC without gaps or stretched intervals.
    """
    if len(slots) != SLOTS_PER_DAY:
        raise ValueError(f"Expected {SLOTS_PER_DAY} slots, got {len(slots)}")

    slices_by_fold: list[tuple[datetime, datetime, str]] = []
    # Collect normal/fold=0 slices first, buffering any repeated-hour fold=1 slices
    # so chronological UTC order is preserved when the clock jumps back at 04:00->03:00.
    pending_fold_1: list[tuple[datetime, datetime, str]] = []
    delta = timedelta(minutes=SLOT_MINUTES)

    for idx, ch in enumerate(slots):
        local_naive = datetime.combine(day, time(idx // 2, (idx % 2) * SLOT_MINUTES))
        if not _local_exists(local_naive, tz):
            continue

        start_utc = local_naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
        if _is_ambiguous_hour(local_naive, tz):
            slices_by_fold.append((start_utc, start_utc + delta, ch))
            fold1_utc = local_naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
            pending_fold_1.append((fold1_utc, fold1_utc + delta, ch))
        else:
            if pending_fold_1:
                slices_by_fold.extend(pending_fold_1)
                pending_fold_1.clear()
            slices_by_fold.append((start_utc, start_utc + delta, ch))

    if pending_fold_1:
        slices_by_fold.extend(pending_fold_1)

    return slices_by_fold


def _merge_interval_stream(slices: Iterable[tuple[datetime, datetime, str]]) -> list[Interval]:
    """Merge contiguous UTC slices of the same outage character into Intervals."""
    intervals: list[Interval] = []
    for start_utc, end_utc, ch in slices:
        if ch not in _OUTAGE_CHARS:
            continue
        if intervals and intervals[-1].kind == ch and intervals[-1].end == start_utc:
            prev = intervals[-1]
            intervals[-1] = Interval(start=prev.start, end=end_utc, kind=ch)
        else:
            intervals.append(Interval(start=start_utc, end=end_utc, kind=ch))
    return intervals


def day_intervals(day: date, slots: str, tz: ZoneInfo = KYIV_TZ) -> list[Interval]:
    """Convert a single day's 48-char string into UTC outage/possible Intervals."""
    return _merge_interval_stream(_day_utc_slices(day, slots, tz))


def build_intervals(days: Mapping[str, str], tz: ZoneInfo = KYIV_TZ) -> list[Interval]:
    """Build continuous UTC Intervals across multiple ISO-dated days, merging midnight seams."""
    all_slices: list[tuple[datetime, datetime, str]] = []
    for day_str in sorted(days):
        slots = days[day_str]
        if len(slots) != SLOTS_PER_DAY:
            continue
        day = date.fromisoformat(day_str)
        all_slices.extend(_day_utc_slices(day, slots, tz))
    return _merge_interval_stream(all_slices)


def merge_days(
    old_days: Mapping[str, str],
    new_days: Mapping[str, str],
    today: date,
    cur_slot: int,
    *,
    clear_future_today: bool = False,
    clear_missing_days: bool = False,
    history_days: int = HISTORY_DAYS,
) -> dict[str, str]:
    """Merge historical and fresh schedules without rewriting already-elapsed slots."""
    cutoff = today - timedelta(days=history_days)
    today_iso = today.isoformat()
    clamped_slot = max(0, min(SLOTS_PER_DAY, cur_slot))
    merged: dict[str, str] = {}

    for day_str, old_slots in old_days.items():
        try:
            day = date.fromisoformat(day_str)
        except ValueError:
            continue
        if day < cutoff or len(old_slots) != SLOTS_PER_DAY:
            continue
        if day < today:
            merged[day_str] = old_slots
        elif day == today:
            merged[day_str] = old_slots
        elif not clear_missing_days and day_str not in new_days:
            merged[day_str] = old_slots

    for day_str, new_slots in new_days.items():
        try:
            day = date.fromisoformat(day_str)
        except ValueError:
            continue
        if day < today or len(new_slots) != SLOTS_PER_DAY:
            continue
        if day == today:
            old_today = merged.get(today_iso)
            if clear_future_today:
                base_prefix = old_today[:clamped_slot] if old_today else SLOT_ON * clamped_slot
                merged[today_iso] = base_prefix + SLOT_ON * (SLOTS_PER_DAY - clamped_slot)
            elif old_today is not None:
                merged[today_iso] = old_today[:clamped_slot] + new_slots[clamped_slot:]
            else:
                merged[today_iso] = new_slots
        elif not clear_future_today:
            merged[day_str] = new_slots

    if clear_future_today and today_iso in merged:
        prefix = merged[today_iso][:clamped_slot]
        merged[today_iso] = prefix + SLOT_ON * (SLOTS_PER_DAY - clamped_slot)
    if clear_future_today:
        for day_str in [k for k in merged if k > today_iso]:
            del merged[day_str]

    return dict(sorted(merged.items()))


def combine_slots(primary: str, others: Sequence[str], mode: str) -> str:
    """Combine 48-char slot strings from multiple groups according to groups_mode."""
    if len(primary) != SLOTS_PER_DAY:
        raise ValueError(f"Expected {SLOTS_PER_DAY} slots for primary group, got {len(primary)}")

    valid_others = [s for s in others if len(s) == SLOTS_PER_DAY]
    if mode == GROUPS_MODE_MAIN or not valid_others:
        return primary

    all_groups = [primary, *valid_others]
    result: list[str] = []

    for idx in range(SLOTS_PER_DAY):
        chars = [g[idx] for g in all_groups]
        if mode == GROUPS_MODE_ANY:
            if SLOT_OFF in chars:
                result.append(SLOT_OFF)
            elif SLOT_MAYBE in chars:
                result.append(SLOT_MAYBE)
            else:
                result.append(SLOT_ON)
        elif mode == GROUPS_MODE_ALL:
            if all(ch == SLOT_OFF for ch in chars):
                result.append(SLOT_OFF)
            elif any(ch in _OUTAGE_CHARS for ch in chars):
                result.append(SLOT_MAYBE)
            else:
                result.append(SLOT_ON)
        else:
            result.append(primary[idx])

    return "".join(result)


def future_changes(
    old_days: Mapping[str, str],
    new_days: Mapping[str, str],
    today: date,
    cur_slot: int,
) -> bool:
    """Return True if any future schedule slot from today[cur_slot:] onward changed."""
    today_iso = today.isoformat()
    clamped_slot = max(0, min(SLOTS_PER_DAY, cur_slot))

    relevant_dates = {d for d in (*old_days.keys(), *new_days.keys()) if d >= today_iso}
    for day_str in sorted(relevant_dates):
        old_val = old_days.get(day_str)
        new_val = new_days.get(day_str)
        if day_str == today_iso:
            old_tail = old_val[clamped_slot:] if old_val else None
            new_tail = new_val[clamped_slot:] if new_val else None
            if old_tail != new_tail:
                return True
        elif old_val != new_val:
            return True
    return False


def possible_days(
    preset_days: Mapping[int, str],
    start_date: date,
    *,
    days_ahead: int = POSSIBLE_DAYS_AHEAD,
    existing_days: Iterable[date] = (),
) -> dict[str, str]:
    """Generate possible-outage schedules from weekly preset for dates lacking fact data."""
    if not preset_days or days_ahead <= 0:
        return {}

    existing_set = set(existing_days)
    result: dict[str, str] = {}
    for offset in range(days_ahead):
        target = start_date + timedelta(days=offset)
        if target in existing_set:
            continue
        raw_week_slots = preset_days.get(target.isoweekday())
        if raw_week_slots is None or len(raw_week_slots) != SLOTS_PER_DAY:
            continue
        # Weekly preset entries represent potential grey-zone intervals, not confirmed outages.
        normalized = raw_week_slots.replace(SLOT_OFF, SLOT_MAYBE)
        result[target.isoformat()] = normalized
    return result


def summarize_day(day: date, slots: str, tz: ZoneInfo = KYIV_TZ) -> dict[str, object]:
    """Build duration totals and formatted outage/possible interval summaries for one day."""
    intervals = day_intervals(day, slots, tz)
    outage_intervals = [iv for iv in intervals if iv.kind == SLOT_OFF]
    possible_intervals = [iv for iv in intervals if iv.kind == SLOT_MAYBE]

    outage_minutes = sum(iv.duration_minutes for iv in outage_intervals)
    possible_minutes = sum(iv.duration_minutes for iv in possible_intervals)

    return {
        "date": day.isoformat(),
        "outage_hours": round(outage_minutes / 60, 2),
        "possible_hours": round(possible_minutes / 60, 2),
        "outages_text": ", ".join(iv.format_local(tz) for iv in outage_intervals),
        "possible_text": ", ".join(iv.format_local(tz) for iv in possible_intervals),
        "outages": [iv.to_dict(tz) for iv in intervals],
        "schedule": slots,
    }

"""Unit tests for 48-slot schedule math and DST-safe UTC interval builders."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from dtek_client.models import SlotStatus

from custom_components.ua_outages.const import (
    GROUPS_MODE_ALL,
    GROUPS_MODE_ANY,
    GROUPS_MODE_MAIN,
    SLOT_MAYBE,
    SLOT_OFF,
    SLOT_ON,
    SLOTS_PER_DAY,
)
from custom_components.ua_outages.schedule import (
    KYIV_TZ,
    Interval,
    build_intervals,
    combine_slots,
    current_slot,
    day_intervals,
    future_changes,
    merge_days,
    parse_hours,
    possible_days,
    summarize_day,
)


class TestParseHours:
    def test_empty_dict_returns_none(self) -> None:
        assert parse_hours({}) is None

    def test_24_hour_grid_maps_half_hour_transitions(self) -> None:
        raw: dict[str, SlotStatus | str] = {
            "1": SlotStatus.NO,
            "2": SlotStatus.FIRST,
            "3": SlotStatus.SECOND,
            "4": SlotStatus.MAYBE,
            "5": SlotStatus.MFIRST,
            "6": SlotStatus.MSECOND,
            "7": "unknown_val",
        }
        for idx in range(8, 25):
            raw[str(idx)] = SlotStatus.YES

        parsed = parse_hours(raw)
        assert parsed is not None
        assert len(parsed) == SLOTS_PER_DAY
        assert parsed[:14] == "000110???11???"
        assert parsed[14:] == "1" * 34

    def test_48_half_hour_grid_detected_by_keys_and_timezone(self) -> None:
        raw = {str(i): "yes" for i in range(1, 49)}
        raw["1"] = "no"
        raw["2"] = "no"
        raw["48"] = "maybe"

        parsed = parse_hours(raw)
        assert parsed is not None
        assert len(parsed) == SLOTS_PER_DAY
        assert parsed[:2] == "00"
        assert parsed[2:47] == "1" * 45
        assert parsed[47] == "?"

        tz_48 = {str(i): f"{i}" for i in range(1, 49)}
        partial_48 = {"1": "no", "2": "yes"}
        parsed_partial = parse_hours(partial_48, time_zone=tz_48)
        assert parsed_partial is not None
        assert parsed_partial.startswith("01?")


class TestDstAndIntervals:
    def test_spring_dst_23_hour_day_skips_nonexistent_hour(self) -> None:
        # 2026-03-29 in Europe/Kyiv springs forward from 02:59 to 04:00 (23h day).
        slots = list(SLOT_ON * SLOTS_PER_DAY)
        # Mark 02:00-05:00 local (slots 4..9) as outage; 03:00-04:00 does not exist.
        for idx in range(4, 10):
            slots[idx] = SLOT_OFF
        day_str = "".join(slots)

        intervals = day_intervals(date(2026, 3, 29), day_str, KYIV_TZ)
        assert len(intervals) == 1
        iv = intervals[0]
        assert iv.start == datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
        assert iv.end == datetime(2026, 3, 29, 2, 0, tzinfo=UTC)
        assert iv.duration_minutes == 120

    def test_autumn_dst_25_hour_day_covers_both_folds(self) -> None:
        # 2026-10-25 in Europe/Kyiv repeats 03:00-04:00 (25h day).
        slots = list(SLOT_ON * SLOTS_PER_DAY)
        # Slots 6..7 cover 03:00-04:00 local, which occurs twice in UTC (00:00..02:00 UTC).
        slots[6] = SLOT_OFF
        slots[7] = SLOT_OFF
        day_str = "".join(slots)

        intervals = day_intervals(date(2026, 10, 25), day_str, KYIV_TZ)
        assert len(intervals) == 1
        iv = intervals[0]
        assert iv.start == datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
        assert iv.end == datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
        assert iv.duration_minutes == 120

    def test_midnight_boundary_merges_contiguous_days(self) -> None:
        day1 = (SLOT_ON * 44) + (SLOT_OFF * 4)
        day2 = (SLOT_OFF * 4) + (SLOT_ON * 44)
        intervals = build_intervals({"2026-11-16": day1, "2026-11-17": day2}, KYIV_TZ)
        assert len(intervals) == 1
        assert intervals[0].duration_minutes == 240
        assert intervals[0].start.astimezone(KYIV_TZ).hour == 22
        assert intervals[0].end.astimezone(KYIV_TZ).hour == 2

    def test_invalid_slot_length_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Expected 48 slots"):
            day_intervals(date(2026, 11, 16), "101", KYIV_TZ)


class TestMergeCombineAndForecast:
    def test_merge_days_preserves_elapsed_slots_and_respects_history(self) -> None:
        today = date(2026, 11, 16)
        old_days = {
            "2026-11-01": SLOT_OFF * SLOTS_PER_DAY,
            "2026-11-15": SLOT_OFF * SLOTS_PER_DAY,
            "2026-11-16": (SLOT_OFF * 10) + (SLOT_ON * 38),
            "2026-11-17": (SLOT_OFF * 4) + (SLOT_ON * 44),
        }
        new_days = {
            "2026-11-16": (SLOT_ON * 10) + (SLOT_OFF * 38),
        }

        merged = merge_days(old_days, new_days, today, cur_slot=10, history_days=7)
        assert "2026-11-01" not in merged
        assert merged["2026-11-15"] == SLOT_OFF * SLOTS_PER_DAY
        assert merged["2026-11-16"] == (SLOT_OFF * 10) + (SLOT_OFF * 38)
        assert merged["2026-11-17"] == (SLOT_OFF * 4) + (SLOT_ON * 44)

    def test_merge_days_clear_future_when_table_hidden(self) -> None:
        today = date(2026, 11, 16)
        old_days = {
            "2026-11-16": (SLOT_OFF * 10) + (SLOT_OFF * 38),
            "2026-11-17": SLOT_OFF * SLOTS_PER_DAY,
        }
        merged = merge_days(
            old_days,
            {"2026-11-16": SLOT_OFF * SLOTS_PER_DAY},
            today,
            cur_slot=10,
            clear_future_today=True,
        )
        assert merged["2026-11-16"] == (SLOT_OFF * 10) + (SLOT_ON * 38)
        assert "2026-11-17" not in merged

    def test_combine_slots_modes(self) -> None:
        primary = (SLOT_OFF * 4) + (SLOT_ON * 44)
        other = (SLOT_OFF * 2) + (SLOT_ON * 2) + (SLOT_OFF * 2) + (SLOT_ON * 42)

        assert combine_slots(primary, [other], GROUPS_MODE_MAIN) == primary

        any_res = combine_slots(primary, [other], GROUPS_MODE_ANY)
        assert any_res[:6] == SLOT_OFF * 6
        assert any_res[6:] == SLOT_ON * 42

        all_res = combine_slots(primary, [other], GROUPS_MODE_ALL)
        assert all_res[:2] == SLOT_OFF * 2
        assert all_res[2:6] == SLOT_MAYBE * 4
        assert all_res[6:] == SLOT_ON * 42

    def test_future_changes_ignores_elapsed_slots(self) -> None:
        today = date(2026, 11, 16)
        old = {"2026-11-16": (SLOT_OFF * 10) + (SLOT_ON * 38)}
        same_future = {"2026-11-16": (SLOT_ON * 10) + (SLOT_ON * 38)}
        changed_future = {"2026-11-16": (SLOT_ON * 10) + (SLOT_OFF * 38)}

        assert future_changes(old, same_future, today, cur_slot=10) is False
        assert future_changes(old, changed_future, today, cur_slot=10) is True

    def test_possible_days_skips_existing_fact_days(self) -> None:
        preset = {
            1: (SLOT_OFF * 4) + (SLOT_ON * 44),
            2: (SLOT_MAYBE * 2) + (SLOT_ON * 46),
        }
        start = date(2026, 11, 16)  # Monday (isoweekday == 1)
        res = possible_days(preset, start, days_ahead=3, existing_days=[start])
        assert start.isoformat() not in res
        assert res["2026-11-17"] == (SLOT_MAYBE * 2) + (SLOT_ON * 46)

    def test_summarize_day_and_interval_helpers(self) -> None:
        day = date(2026, 11, 16)
        slots = (SLOT_OFF * 4) + (SLOT_MAYBE * 2) + (SLOT_ON * 42)
        summary = summarize_day(day, slots, KYIV_TZ)
        assert summary["outage_hours"] == 2.0
        assert summary["possible_hours"] == 1.0
        assert summary["outages_text"] == "00:00-02:00"
        assert summary["possible_text"] == "02:00-03:00"
        assert current_slot(datetime(2026, 11, 16, 14, 45, tzinfo=ZoneInfo("Europe/Kyiv"))) == 29

        iv = Interval(
            start=datetime(2026, 11, 16, 20, 0, tzinfo=UTC),
            end=datetime(2026, 11, 16, 22, 0, tzinfo=UTC),
            kind=SLOT_OFF,
        )
        assert iv.contains(datetime(2026, 11, 16, 21, 0, tzinfo=UTC)) is True
        assert iv.format_local(KYIV_TZ) == "22:00-24:00"

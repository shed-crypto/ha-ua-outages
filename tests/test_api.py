"""Unit tests for provider adapter and Baskerville42 mirror parsing."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from dtek_client.exceptions import DtekBlockedError, DtekDataError
from dtek_client.models import HomeNumResponse
from dtek_client.stub_client import StubDtekClient

from custom_components.ua_outages.api import (
    AddressOutage,
    build_user_agent,
    classify_outage_kind,
    clear_dtek_clients,
    fetch_dtek_snapshot,
    fetch_mirror_snapshot,
    get_dtek_client,
    is_dtek_client_error,
    normalize_group_ids,
    parse_house_datetime,
    parse_update_timestamp,
    snapshot_from_home_num,
    snapshot_from_mirror_payload,
)
from custom_components.ua_outages.const import (
    OUTAGE_KIND_EMERGENCY,
    OUTAGE_KIND_NONE,
    OUTAGE_KIND_OTHER,
    OUTAGE_KIND_PLANNED,
    OUTAGE_KIND_STABILIZATION,
    SLOTS_PER_DAY,
)
from custom_components.ua_outages.schedule import KYIV_TZ

FIXTURE_HOME_NUM = Path(__file__).resolve().parent / "fixtures" / "home_num_response.json"


class TestHelpersAndTimestamps:
    def test_build_user_agent(self) -> None:
        assert build_user_agent("0.1.0") == "ha-ua-outages/0.1.0 HomeAssistant"
        assert build_user_agent("  ") == "ha-ua-outages/0.0.0 HomeAssistant"

    def test_normalize_group_ids_deduplicates_preserving_order(self) -> None:
        assert normalize_group_ids([" GPV3.1 ", "GPV3.2", "GPV3.1", ""]) == ["GPV3.1", "GPV3.2"]

    def test_classify_outage_kind(self) -> None:
        assert classify_outage_kind("") == OUTAGE_KIND_NONE
        assert classify_outage_kind("Стабілізаційне відключення") == OUTAGE_KIND_STABILIZATION
        assert classify_outage_kind("Аварійні роботи") == OUTAGE_KIND_EMERGENCY
        assert classify_outage_kind("Екстрене відключення") == OUTAGE_KIND_EMERGENCY
        assert classify_outage_kind("Планові ремонтні роботи") == OUTAGE_KIND_PLANNED
        assert classify_outage_kind("Технічне обслуговування") == OUTAGE_KIND_OTHER

    def test_parse_house_datetime_supports_both_dtek_formats(self) -> None:
        dt_time_first = parse_house_datetime("10:30 16.11.2026")
        dt_date_first = parse_house_datetime("16.11.2026 10:30")
        assert dt_time_first == datetime(2026, 11, 16, 10, 30, tzinfo=KYIV_TZ)
        assert dt_date_first == dt_time_first
        assert parse_house_datetime("") is None
        assert parse_house_datetime("not a date") is None

    def test_parse_update_timestamp_handles_dtek_iso_and_stub(self) -> None:
        dtek_dt = parse_update_timestamp("26.03.2026 13:45")
        assert dtek_dt == datetime(2026, 3, 26, 11, 45, tzinfo=UTC)

        iso_dt = parse_update_timestamp("2026-11-16T11:41:56.430Z")
        assert iso_dt == datetime(2026, 11, 16, 11, 41, 56, 430000, tzinfo=UTC)

        assert parse_update_timestamp("Stub data") is None
        assert parse_update_timestamp(None) is None

    def test_address_outage_roundtrip(self) -> None:
        orig = AddressOutage(
            reason="Планові роботи",
            kind=OUTAGE_KIND_PLANNED,
            start=datetime(2026, 11, 16, 9, 0, tzinfo=KYIV_TZ),
            end=datetime(2026, 11, 16, 17, 0, tzinfo=KYIV_TZ),
            outage_type="1",
        )
        restored = AddressOutage.from_dict(orig.to_dict())
        assert restored == orig

    def test_is_dtek_client_error(self) -> None:
        assert is_dtek_client_error(DtekBlockedError("waf")) is True
        assert is_dtek_client_error(DtekDataError("bad")) is True
        assert is_dtek_client_error(ValueError("other")) is False


class TestSnapshotConversion:
    async def test_stub_client_snapshot_24_slots_and_stub_timestamp(self) -> None:
        async with StubDtekClient("krem") as client:
            response = await client.get_home_num("м. Українка", "вул. Юності")
        snapshot = snapshot_from_home_num(response, house_number="1")

        assert snapshot.source == "dtek"
        assert snapshot.fact_update == "Stub data"
        assert snapshot.fact_updated_at is None
        assert snapshot.groups_at_address == ["GPV3.1"]
        assert snapshot.publication is not None
        assert snapshot.publication.daily_shown is True
        assert "GPV3.1" in snapshot.days_by_group
        for day_slots in snapshot.days_by_group["GPV3.1"].values():
            assert len(day_slots) == SLOTS_PER_DAY
        assert len(snapshot.preset_by_group["GPV3.1"]) == 7

    def test_fixture_snapshot_48_slots_lookalike_and_house_outage(self) -> None:
        raw = json.loads(FIXTURE_HOME_NUM.read_text(encoding="utf-8"))
        response = HomeNumResponse.model_validate(raw)

        multi = snapshot_from_home_num(response, house_number=" 3/а ")
        assert multi.groups_at_address == ["GPV3.1", "GPV3.2"]
        assert multi.fact_update == "26.03.2026 13:45"
        assert multi.fact_updated_at == datetime(2026, 3, 26, 11, 45, tzinfo=UTC)
        assert multi.today == date(2026, 3, 26)
        assert len(multi.days_by_group["GPV3.1"]["2026-03-26"]) == SLOTS_PER_DAY

        planned = snapshot_from_home_num(response, house_number="7")
        assert planned.address_outage is not None
        assert planned.address_outage.kind == OUTAGE_KIND_PLANNED
        assert planned.address_outage.start == datetime(2026, 3, 26, 9, 0, tzinfo=KYIV_TZ)
        assert planned.address_outage.end == datetime(2026, 3, 26, 18, 0, tzinfo=KYIV_TZ)

    def test_house_outage_drops_end_earlier_than_start_and_marks_cek(self) -> None:
        raw = {
            "data": {
                "12а": [
                    {"sub_type_reason": ["GPV1.1"], "sub_type": ""},
                    {
                        "sub_type_reason": ["GPV1.2"],
                        "sub_type": "Аварійні роботи",
                        "start_date": "16:00 16.11.2026",
                        "end_date": "12:00 16.11.2026",
                        "type": "2",
                        "cek": True,
                    },
                ]
            }
        }
        response = HomeNumResponse.model_validate(raw)
        snap = snapshot_from_home_num(response, house_number="12A")
        assert snap.external_operator is True
        assert snap.groups_at_address == ["GPV1.1", "GPV1.2"]
        assert snap.address_outage is not None
        assert snap.address_outage.kind == OUTAGE_KIND_EMERGENCY
        assert snap.address_outage.end is None

    def test_mirror_payload_uses_update_fact_and_iso_fallback(self) -> None:
        mirror_raw = {
            "lastUpdated": "2026-11-16T11:41:56.430Z",
            "fact": {
                "today": 1794780000,
                "updateFact": "16.11.2026 13:40",
                "data": {
                    "1794780000": {
                        "GPV3.1": {str(i): "no" if i <= 4 else "yes" for i in range(1, 25)}
                    }
                },
            },
            "preset": {
                "data": {"GPV3.1": {"1": {str(i): "yes" for i in range(1, 25)}}},
                "time_zone": {str(i): f"{i - 1:02d}-{i:02d}" for i in range(1, 25)},
                "sch_names": {"GPV3.1": "Черга 3.1"},
                "days": {"1": "Понеділок"},
            },
        }
        snap = snapshot_from_mirror_payload(mirror_raw)
        assert snap.source == "mirror"
        assert snap.publication is None
        assert snap.fact_update == "16.11.2026 13:40"
        assert snap.fact_updated_at == datetime(2026, 11, 16, 11, 40, tzinfo=UTC)
        assert "GPV3.1" in snap.days_by_group
        assert snap.sch_names == {"GPV3.1": "Черга 3.1"}


class TestAsyncClientAndMirrorFetch:
    def test_client_cache_and_cleanup(self) -> None:
        hass = MagicMock()
        hass.data = {}
        with patch("custom_components.ua_outages.api.async_create_clientsession"):
            c1 = get_dtek_client(hass, "krem", user_agent="ua/1")
            c2 = get_dtek_client(hass, "krem", user_agent="ua/2")
            assert c1 is c2
            clear_dtek_clients(hass)
            c3 = get_dtek_client(hass, "krem")
            assert c3 is not c1

    async def test_fetch_dtek_snapshot_calls_get_home_num_with_defaults(self) -> None:
        raw = json.loads(FIXTURE_HOME_NUM.read_text(encoding="utf-8"))
        response = HomeNumResponse.model_validate(raw)
        mock_client = MagicMock()
        mock_client.get_home_num = AsyncMock(return_value=response)

        snap = await fetch_dtek_snapshot(mock_client, "м. Українка", "вул. Юності", "1")
        mock_client.get_home_num.assert_awaited_once_with("м. Українка", "вул. Юності")
        assert snap.groups_at_address == ["GPV3.1"]

    async def test_fetch_mirror_snapshot_handles_dem_and_krem(self) -> None:
        hass = MagicMock()
        assert await fetch_mirror_snapshot(hass, "dem") is None

        mirror_raw = {
            "lastUpdated": "2026-11-16T12:00:00Z",
            "fact": {"today": 1794780000, "data": {}},
        }
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = AsyncMock(return_value=mirror_raw)
        get_cm = MagicMock()
        get_cm.__aenter__ = AsyncMock(return_value=resp)
        get_cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=get_cm)

        with patch(
            "custom_components.ua_outages.api.async_get_clientsession",
            return_value=session,
        ):
            snap = await fetch_mirror_snapshot(hass, "krem")

        assert snap is not None
        assert snap.source == "mirror"
        assert snap.fact_updated_at == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)

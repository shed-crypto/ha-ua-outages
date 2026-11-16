"""Tests for config_flow helpers, setup wizard, reconfigure, and options flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from dtek_client.exceptions import DtekBlockedError, DtekDataError
from dtek_client.models import HomeNumResponse
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ua_outages.config_flow import (
    build_street_choices,
    group_settlements,
    natural_house_sort_key,
    resolve_settlement_input,
    resolve_street_input,
    uk_sort_key,
)
from custom_components.ua_outages.const import (
    CONF_EXCLUDED_GROUPS,
    CONF_EXTRA_GROUPS,
    CONF_GROUP,
    CONF_GROUPS_MODE,
    CONF_HOUSE,
    CONF_LANGUAGE,
    CONF_MANUAL,
    CONF_MODE,
    CONF_NAME,
    CONF_SETTLEMENT,
    CONF_SITE_KEY,
    CONF_STREET,
    CONF_UPDATE_INTERVAL,
    CONF_USE_MIRROR,
    CONF_WARN_BEFORE_END,
    CONF_WARN_BEFORE_START,
    DOMAIN,
    GROUPS_MODE_ANY,
    MODE_ADDRESS,
    MODE_GROUP_ONLY,
)


def _sample_home_response() -> HomeNumResponse:
    return HomeNumResponse.model_validate(
        {
            "data": {
                "1": {"sub_type_reason": ["GPV3.1"]},
                "3/A": {"sub_type_reason": ["GPV3.1", "GPV3.2"]},
                "5": {"sub_type_reason": []},
            },
            "preset": {
                "data": {"GPV3.1": {"1": {"1": "yes"}}, "GPV3.2": {"1": {"1": "no"}}},
                "time_zone": {"1": "00-01"},
                "sch_names": {
                    "GPV3.1": "Черга 3.1",
                    "GPV3.2": "Черга 3.2",
                },
                "days": {"1": "Понеділок"},
            },
        }
    )


class TestCollationAndSearchHelpers:
    def test_ukrainian_alphabet_order(self) -> None:
        words = ["їжак", "іскра", "ґанок", "година", "єнот", "енергія"]
        assert sorted(words, key=uk_sort_key) == [
            "година",
            "ґанок",
            "енергія",
            "єнот",
            "іскра",
            "їжак",
        ]

    def test_natural_house_sort_order(self) -> None:
        houses = ["10/10", "10/2", "10а", "2", "10", "1"]
        assert sorted(houses, key=natural_house_sort_key) == [
            "1",
            "2",
            "10",
            "10а",
            "10/2",
            "10/10",
        ]

    def test_duplicate_settlement_grouping_and_street_disambiguation(self) -> None:
        all_streets = {
            "Софіївська Борщагівка": ["Без вулиці"],
            "с. Софіївська Борщагівка": ["Без вулиці", "вул. Київська"],
            "м. Українка": ["вул. Юності"],
        }
        grouped = group_settlements(all_streets)
        assert list(grouped.keys()) == ["с. Софіївська Борщагівка", "м. Українка"]
        assert set(grouped["с. Софіївська Борщагівка"]) == {
            "с. Софіївська Борщагівка",
            "Софіївська Борщагівка",
        }

        resolved, _ = resolve_settlement_input("Українка", grouped)
        assert resolved == "м. Українка"

        choices = build_street_choices(grouped["с. Софіївська Борщагівка"], all_streets)
        labels = [label for _enc, label, _raw in choices]
        assert "Без вулиці (с. Софіївська Борщагівка)" in labels
        assert "Без вулиці (Софіївська Борщагівка)" in labels
        assert "вул. Київська" in labels

        street_res, _ = resolve_street_input("Київська", choices)
        assert street_res == ("с. Софіївська Борщагівка", "вул. Київська")


class TestConfigFlowPaths:
    async def test_krem_full_address_flow_and_lookalike_house(
        self,
        hass: HomeAssistant,
    ) -> None:
        mock_client = MagicMock()
        mock_client.get_all_streets = AsyncMock(
            return_value={
                "м. Українка": ["вул. Юності"],
                "м. Обухів": ["вул. Миру"],
            }
        )
        mock_client.get_home_num = AsyncMock(return_value=_sample_home_response())

        with patch(
            "custom_components.ua_outages.config_flow.get_dtek_client", return_value=mock_client
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "user"

            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_SITE_KEY: "krem", CONF_MODE: MODE_ADDRESS},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "settlement"

            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_SETTLEMENT: "Українка"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "street"

            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_STREET: "Юності"},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "house"

            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_HOUSE: "1", CONF_NAME: "Home", CONF_LANGUAGE: "uk"},
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert result["title"] == "Home"
            assert result["data"][CONF_GROUP] == "GPV3.1"
            assert result["data"][CONF_MANUAL] is False

    async def test_kem_auto_skips_settlement_step(self, hass: HomeAssistant) -> None:
        mock_client = MagicMock()
        mock_client.get_all_streets = AsyncMock(return_value={"м. Київ": ["вул. Хрещатик"]})
        mock_client.get_home_num = AsyncMock(return_value=_sample_home_response())

        with patch(
            "custom_components.ua_outages.config_flow.get_dtek_client", return_value=mock_client
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER},
                data={CONF_SITE_KEY: "kem", CONF_MODE: MODE_ADDRESS},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "street"

            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_STREET: "вул. Хрещатик"},
            )
            assert result["step_id"] == "house"

            # Lookalike query "3/a" hits multi-group "3/A" and routes to group step.
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_HOUSE: "3/а", CONF_LANGUAGE: "en"},
            )
            assert result["step_id"] == "group"

            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_GROUP: "GPV3.2"},
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert result["data"][CONF_HOUSE] == "3/A"
            assert result["data"][CONF_GROUP] == "GPV3.2"
            assert result["data"][CONF_MANUAL] is False

    async def test_unreachable_fallback_and_dem_group_only(self, hass: HomeAssistant) -> None:
        mock_client = MagicMock()
        mock_client.get_all_streets = AsyncMock(side_effect=DtekBlockedError("waf"))

        with patch(
            "custom_components.ua_outages.config_flow.get_dtek_client", return_value=mock_client
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER},
                data={CONF_SITE_KEY: "dem", CONF_MODE: MODE_ADDRESS},
            )
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "unreachable"
            assert "mirror" in result["description_placeholders"]["mirror_warning"].lower()

            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={"action": MODE_GROUP_ONLY},
            )
            assert result["step_id"] == "group_only"

            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_GROUP: "GPV4.1", CONF_NAME: "Donetsk", CONF_LANGUAGE: "uk"},
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert result["data"][CONF_SITE_KEY] == "dem"
            assert result["data"][CONF_MODE] == MODE_GROUP_ONLY

    async def test_dtek_data_error_on_street_allows_manual_house_and_reconfigure(
        self,
        hass: HomeAssistant,
        address_entry: MockConfigEntry,
    ) -> None:
        address_entry.add_to_hass(hass)
        mock_client = MagicMock()
        mock_client.get_all_streets = AsyncMock(return_value={"м. Київ": ["Без вулиці"]})
        mock_client.get_home_num = AsyncMock(side_effect=DtekDataError("result=false"))

        with (
            patch(
                "custom_components.ua_outages.config_flow.get_dtek_client", return_value=mock_client
            ),
            patch.object(hass.config_entries, "async_reload", return_value=True),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={
                    "source": config_entries.SOURCE_RECONFIGURE,
                    "entry_id": address_entry.entry_id,
                },
            )
            assert result["step_id"] == "user"
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_SITE_KEY: "kem", CONF_MODE: MODE_ADDRESS},
            )
            assert result["step_id"] == "street"
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_STREET: "Без вулиці"},
            )
            assert result["step_id"] == "house"
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_HOUSE: "99", CONF_LANGUAGE: "en"},
            )
            assert result["step_id"] == "group"
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={CONF_GROUP: "GPV2.1"},
            )
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "reconfigure_successful"
            assert address_entry.data[CONF_SITE_KEY] == "kem"
            assert address_entry.data[CONF_GROUP] == "GPV2.1"
            assert address_entry.data[CONF_MANUAL] is True


class TestOptionsFlow:
    async def test_options_hides_mirror_for_dem_and_converts_warnings(
        self,
        hass: HomeAssistant,
    ) -> None:
        dem_entry = MockConfigEntry(
            domain=DOMAIN,
            title="Dem",
            unique_id="dem|group|GPV3.1",
            data={
                CONF_SITE_KEY: "dem",
                CONF_MODE: MODE_GROUP_ONLY,
                CONF_GROUP: "GPV3.1",
                CONF_MANUAL: True,
            },
        )
        dem_entry.add_to_hass(hass)

        result = await hass.config_entries.options.async_init(dem_entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        schema_keys = {str(k) for k in result["data_schema"].schema}
        assert CONF_USE_MIRROR not in schema_keys
        assert CONF_EXTRA_GROUPS in schema_keys

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_UPDATE_INTERVAL: 10,
                CONF_WARN_BEFORE_START: ["15", "60"],
                CONF_WARN_BEFORE_END: ["10"],
                CONF_GROUPS_MODE: GROUPS_MODE_ANY,
                CONF_EXTRA_GROUPS: ["GPV3.2"],
                CONF_LANGUAGE: "en",
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        saved: dict[str, Any] = result["data"]
        assert saved[CONF_UPDATE_INTERVAL] == 10
        assert saved[CONF_WARN_BEFORE_START] == [60, 15]
        assert saved[CONF_WARN_BEFORE_END] == [10]
        assert saved[CONF_USE_MIRROR] is False
        assert saved[
            CONF_EXCLUDED_GROUPS if CONF_EXCLUDED_GROUPS in saved else CONF_EXTRA_GROUPS
        ] == ["GPV3.2"]

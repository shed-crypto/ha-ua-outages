"""Config and options flows for the Ukraine Power Outages integration."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from dtek_client.exceptions import DtekBlockedError, DtekClientError, DtekDataError
from dtek_client.models import HomeNumResponse
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .api import get_dtek_client
from .const import (
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
    CONF_SHOW_POSSIBLE,
    CONF_SITE_KEY,
    CONF_STREET,
    CONF_UPDATE_INTERVAL,
    CONF_USE_MIRROR,
    CONF_WARN_BEFORE_END,
    CONF_WARN_BEFORE_START,
    DEFAULT_GROUPS,
    DEFAULT_GROUPS_MODE,
    DEFAULT_LANGUAGE,
    DEFAULT_SHOW_POSSIBLE,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_USE_MIRROR,
    DEFAULT_WARN_BEFORE_END,
    DEFAULT_WARN_BEFORE_START,
    DOMAIN,
    DTEK_SITES,
    GROUPS_MODES,
    LANGUAGES,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    MIRROR_URLS,
    MODE_ADDRESS,
    MODE_GROUP_ONLY,
    REGION_NAMES,
    REGION_NAMES_UA,
    WARN_MINUTE_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

_UK_ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
_UK_RANK: dict[str, int] = {ch: idx for idx, ch in enumerate(_UK_ALPHABET)}

_SETTLEMENT_PREFIXES: tuple[str, ...] = (
    "м. ",
    "м.",
    "с. ",
    "с.",
    "смт ",
    "смт. ",
    "с-ще ",
    "селище ",
)

_STREET_PREFIXES: tuple[str, ...] = (
    "вул. ",
    "вул.",
    "вулиця ",
    "просп. ",
    "проспект ",
    "пр. ",
    "пров. ",
    "провулок ",
    "бульв. ",
    "бульвар ",
    "б-р ",
    "пл. ",
    "площа ",
    "наб. ",
    "набережна ",
    "шосе ",
    "узвіз ",
    "тупик ",
)

_HOUSE_SPLIT_RE = re.compile(r"(\d+)")


def uk_sort_key(text: str) -> tuple[tuple[int, int | str], ...]:
    """Build a collation key placing Ukrainian letters in proper alphabetical order."""
    result: list[tuple[int, int | str]] = []
    for ch in text.strip().casefold():
        if ch in _UK_RANK:
            result.append((1, _UK_RANK[ch]))
        elif ch.isdigit():
            result.append((0, int(ch)))
        else:
            result.append((2, ch))
    return tuple(result)


def natural_house_sort_key(
    house: str,
) -> tuple[tuple[int, int | tuple[tuple[int, int | str], ...]], ...]:
    """Sort house numbers naturally by integer chunks then Ukrainian letters."""
    parts: list[tuple[int, int | tuple[tuple[int, int | str], ...]]] = []
    for chunk in _HOUSE_SPLIT_RE.split(house.strip()):
        if not chunk:
            continue
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        else:
            parts.append((1, uk_sort_key(chunk)))
    return tuple(parts)


def _strip_prefix(value: str, prefixes: tuple[str, ...]) -> str:
    """Strip common Ukrainian settlement or street type prefixes case-insensitively."""
    cleaned = " ".join(value.strip().split())
    lowered = cleaned.casefold()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return cleaned[len(prefix) :].strip()
    return cleaned


def group_settlements(all_streets: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """Group duplicate DTEK settlement names sharing the same stripped core name.

    For example, ``'с. Софіївська Борщагівка'`` and ``'Софіївська Борщагівка'``
    merge under a single UI option while keeping all raw keys for street lookup.
    """
    by_core: dict[str, list[str]] = {}
    for raw_name in all_streets:
        core = _strip_prefix(raw_name, _SETTLEMENT_PREFIXES).casefold()
        by_core.setdefault(core, []).append(raw_name)

    grouped: dict[str, list[str]] = {}
    for raw_names in by_core.values():
        sorted_raw = sorted(
            raw_names,
            key=lambda s: (
                0 if s.casefold() != _strip_prefix(s, _SETTLEMENT_PREFIXES).casefold() else 1,
                uk_sort_key(s),
            ),
        )
        canonical = sorted_raw[0]
        grouped[canonical] = sorted_raw

    return dict(
        sorted(
            grouped.items(),
            key=lambda item: (
                uk_sort_key(_strip_prefix(item[0], _SETTLEMENT_PREFIXES)),
                uk_sort_key(item[0]),
            ),
        )
    )


def build_street_choices(
    raw_settlements: list[str],
    all_streets: Mapping[str, list[str]],
) -> list[tuple[str, str, str]]:
    """Build sorted ``(encoded_value, display_label, raw_street)`` tuples for street selection."""
    entries: list[tuple[str, str]] = []
    name_counts: dict[str, int] = {}
    for raw_settlement in raw_settlements:
        for street in all_streets.get(raw_settlement, []):
            entries.append((raw_settlement, street))
            name_counts[street] = name_counts.get(street, 0) + 1

    multi_settlement = len(raw_settlements) > 1
    choices: list[tuple[str, str, str]] = []
    seen_values: set[str] = set()
    for raw_settlement, street in entries:
        encoded = f"{raw_settlement}\x1f{street}"
        if encoded in seen_values:
            continue
        seen_values.add(encoded)
        if multi_settlement and name_counts.get(street, 0) > 1:
            label = f"{street} ({raw_settlement})"
        else:
            label = street
        choices.append((encoded, label, street))

    choices.sort(
        key=lambda item: (
            uk_sort_key(_strip_prefix(item[2], _STREET_PREFIXES)),
            uk_sort_key(item[2]),
            uk_sort_key(item[1]),
        )
    )
    return choices


def resolve_settlement_input(
    typed: str,
    grouped: Mapping[str, list[str]],
) -> tuple[str | None, list[str]]:
    """Resolve user settlement input (with or without type prefix) to canonical key."""
    cleaned = typed.strip()
    if not cleaned:
        return None, []
    if cleaned in grouped:
        return cleaned, []

    lowered = cleaned.casefold()
    stripped_query = _strip_prefix(cleaned, _SETTLEMENT_PREFIXES).casefold()

    exact_ci = [k for k in grouped if k.casefold() == lowered]
    if len(exact_ci) == 1:
        return exact_ci[0], []

    core_matches = [
        k for k in grouped if _strip_prefix(k, _SETTLEMENT_PREFIXES).casefold() == stripped_query
    ]
    if len(core_matches) == 1:
        return core_matches[0], []
    if len(core_matches) > 1:
        return None, core_matches

    partial = [
        k
        for k in grouped
        if lowered in k.casefold()
        or stripped_query in _strip_prefix(k, _SETTLEMENT_PREFIXES).casefold()
    ]
    if len(partial) == 1:
        return partial[0], []
    return None, partial


def resolve_street_input(
    typed: str,
    choices: list[tuple[str, str, str]],
) -> tuple[tuple[str, str] | None, list[tuple[str, str, str]]]:
    """Resolve encoded street value or prefix-less street query to ``(settlement, street)``."""
    cleaned = typed.strip()
    if not cleaned:
        return None, []

    for encoded, _label, _street in choices:
        if cleaned == encoded:
            raw_settlement, raw_street = encoded.split("\x1f", 1)
            return (raw_settlement, raw_street), []

    lowered = cleaned.casefold()
    stripped_query = _strip_prefix(cleaned, _STREET_PREFIXES).casefold()

    exact_matches = [
        item for item in choices if item[1].casefold() == lowered or item[2].casefold() == lowered
    ]
    if len(exact_matches) == 1:
        raw_settlement, raw_street = exact_matches[0][0].split("\x1f", 1)
        return (raw_settlement, raw_street), []
    if len(exact_matches) > 1:
        return None, exact_matches

    core_matches = [
        item
        for item in choices
        if _strip_prefix(item[2], _STREET_PREFIXES).casefold() == stripped_query
    ]
    if len(core_matches) == 1:
        raw_settlement, raw_street = core_matches[0][0].split("\x1f", 1)
        return (raw_settlement, raw_street), []
    if len(core_matches) > 1:
        return None, core_matches

    partial = [
        item
        for item in choices
        if lowered in item[1].casefold()
        or stripped_query in _strip_prefix(item[2], _STREET_PREFIXES).casefold()
    ]
    if len(partial) == 1:
        raw_settlement, raw_street = partial[0][0].split("\x1f", 1)
        return (raw_settlement, raw_street), []
    return None, partial


def _format_group_label(group_id: str, sch_names: Mapping[str, str]) -> str:
    """Format group selector option label cleanly."""
    short = group_id.removeprefix("GPV")
    display = sch_names.get(group_id, "").strip()
    if display:
        return f"{short} — {display}"
    return short


class UaOutagesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config flow for Ukraine Power Outages."""

    VERSION = 1

    def __init__(self) -> None:
        self._site_key: str = "krem"
        self._mode: str = MODE_ADDRESS
        self._all_streets: dict[str, list[str]] = {}
        self._grouped_settlements: dict[str, list[str]] = {}
        self._selected_settlement_group: str = ""
        self._settlement: str = ""
        self._street: str = ""
        self._house: str = ""
        self._group: str = ""
        self._manual: bool = False
        self._name: str = ""
        self._language: str = DEFAULT_LANGUAGE
        self._home_response: HomeNumResponse | None = None
        self._candidate_groups: list[str] = []
        self._unreachable_reason: str = "cannot_connect"
        self._narrowed_settlements: list[str] | None = None
        self._narrowed_streets: list[tuple[str, str, str]] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> UaOutagesOptionsFlow:
        """Return the options flow handler for this integration."""
        return UaOutagesOptionsFlow()

    def _default_title(self) -> str:
        """Build a descriptive default ConfigEntry title."""
        if self._mode == MODE_GROUP_ONLY:
            region = REGION_NAMES_UA.get(self._site_key, self._site_key.upper())
            short_grp = self._group.removeprefix("GPV")
            return f"{region} ({short_grp})"
        if len(self._all_streets) == 1:
            return f"{self._street}, {self._house}"
        return f"{self._settlement}, {self._street}, {self._house}"

    def _entry_unique_id(self) -> str:
        """Build deterministic unique_id supporting multi-address setups across Ukraine."""
        if self._mode == MODE_GROUP_ONLY:
            return f"{self._site_key}|group|{self._group}"
        return f"{self._site_key}|{self._settlement}|{self._street}|{self._house}"

    def _entry_data(self) -> dict[str, Any]:
        """Build ConfigEntry data dictionary."""
        if self._mode == MODE_GROUP_ONLY:
            return {
                CONF_SITE_KEY: self._site_key,
                CONF_MODE: MODE_GROUP_ONLY,
                CONF_GROUP: self._group,
                CONF_MANUAL: True,
            }
        return {
            CONF_SITE_KEY: self._site_key,
            CONF_MODE: MODE_ADDRESS,
            CONF_SETTLEMENT: self._settlement,
            CONF_STREET: self._street,
            CONF_HOUSE: self._house,
            CONF_GROUP: self._group,
            CONF_MANUAL: self._manual,
        }

    async def _finish_entry(self) -> ConfigFlowResult:
        """Create a new entry or update existing entry when reconfiguring."""
        unique_id = self._entry_unique_id()
        title = self._name.strip() or self._default_title()
        data = self._entry_data()

        if self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
            if unique_id != entry.unique_id:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
            updated_options = dict(entry.options)
            updated_options[CONF_LANGUAGE] = self._language
            return self.async_update_reload_and_abort(
                entry,
                unique_id=unique_id,
                title=title,
                data=data,
                options=updated_options,
            )

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=title,
            data=data,
            options={CONF_LANGUAGE: self._language},
        )

    async def _load_streets_and_advance(self) -> ConfigFlowResult:
        """Fetch regional street directory and route to settlement, street, or unreachable."""
        client = get_dtek_client(self.hass, self._site_key)
        try:
            all_streets = await client.get_all_streets()
        except DtekBlockedError:
            self._unreachable_reason = "blocked"
            return await self.async_step_unreachable()
        except DtekClientError:
            self._unreachable_reason = "cannot_connect"
            return await self.async_step_unreachable()

        if not all_streets:
            self._unreachable_reason = "no_cities"
            return await self.async_step_unreachable()

        self._all_streets = all_streets
        self._grouped_settlements = group_settlements(all_streets)
        self._narrowed_settlements = None
        self._narrowed_streets = None

        if len(self._grouped_settlements) == 1:
            only_key = next(iter(self._grouped_settlements))
            self._selected_settlement_group = only_key
            self._settlement = self._grouped_settlements[only_key][0]
            return await self.async_step_street()

        return await self.async_step_settlement()

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 1: Choose DTEK region and lookup mode (address or group only)."""
        if user_input is not None:
            self._site_key = str(user_input[CONF_SITE_KEY])
            self._mode = str(user_input[CONF_MODE])
            if self._mode == MODE_GROUP_ONLY:
                return await self.async_step_group_only()
            return await self._load_streets_and_advance()

        region_options = [
            selector.SelectOptionDict(value=key, label=REGION_NAMES.get(key, key))
            for key in DTEK_SITES
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_SITE_KEY, default=self._site_key): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=region_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_MODE, default=self._mode): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[MODE_ADDRESS, MODE_GROUP_ONLY],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="mode",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Entry point for changing address, region, or group without losing history."""
        entry = self._get_reconfigure_entry()
        self._site_key = str(entry.data.get(CONF_SITE_KEY, "krem"))
        self._mode = str(entry.data.get(CONF_MODE, MODE_ADDRESS))
        self._settlement = str(entry.data.get(CONF_SETTLEMENT, ""))
        self._street = str(entry.data.get(CONF_STREET, ""))
        self._house = str(entry.data.get(CONF_HOUSE, ""))
        self._group = str(entry.data.get(CONF_GROUP, ""))
        self._name = entry.title
        self._language = str(entry.options.get(CONF_LANGUAGE, DEFAULT_LANGUAGE))
        return await self.async_step_user(user_input)

    async def async_step_settlement(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 2: Select settlement (auto-skipped when region has only 1 settlement)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            typed = str(user_input.get(CONF_SETTLEMENT, ""))
            resolved, matches = resolve_settlement_input(typed, self._grouped_settlements)
            if resolved is not None:
                self._selected_settlement_group = resolved
                self._settlement = self._grouped_settlements[resolved][0]
                self._narrowed_settlements = None
                return await self.async_step_street()
            if matches:
                self._narrowed_settlements = matches
                errors[CONF_SETTLEMENT] = "ambiguous_settlement"
            else:
                self._narrowed_settlements = None
                errors[CONF_SETTLEMENT] = "settlement_not_found"

        option_keys = self._narrowed_settlements or list(self._grouped_settlements.keys())
        options = [selector.SelectOptionDict(value=k, label=k) for k in option_keys]
        default_val = self._settlement if self._settlement in option_keys else vol.UNDEFINED
        schema = vol.Schema(
            {
                vol.Required(CONF_SETTLEMENT, default=default_val): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="settlement", data_schema=schema, errors=errors)

    async def async_step_street(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 3: Select street within the chosen settlement."""
        raw_settlements = self._grouped_settlements.get(
            self._selected_settlement_group,
            [self._settlement],
        )
        all_choices = build_street_choices(raw_settlements, self._all_streets)
        errors: dict[str, str] = {}

        if user_input is not None:
            typed = str(user_input.get(CONF_STREET, ""))
            resolved, matches = resolve_street_input(typed, all_choices)
            if resolved is not None:
                self._settlement, self._street = resolved
                self._narrowed_streets = None
                client = get_dtek_client(self.hass, self._site_key)
                try:
                    self._home_response = await client.get_home_num(self._settlement, self._street)
                except DtekDataError:
                    self._home_response = None
                except DtekBlockedError:
                    self._unreachable_reason = "blocked"
                    return await self.async_step_unreachable()
                except DtekClientError:
                    self._unreachable_reason = "cannot_connect"
                    return await self.async_step_unreachable()
                return await self.async_step_house()
            if matches:
                self._narrowed_streets = matches
                errors[CONF_STREET] = "ambiguous_street"
            else:
                self._narrowed_streets = None
                errors[CONF_STREET] = "street_not_found"

        active_choices = self._narrowed_streets or all_choices
        options = [
            selector.SelectOptionDict(value=encoded, label=label)
            for encoded, label, _street in active_choices
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_STREET): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="street",
            data_schema=schema,
            errors=errors,
            description_placeholders={"settlement": self._settlement},
        )

    async def async_step_house(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 4: Select or enter house number, entry name, and entity language."""
        errors: dict[str, str] = {}
        houses = self._home_response.houses if self._home_response else {}

        if user_input is not None:
            raw_house = str(user_input.get(CONF_HOUSE, "")).strip()
            self._name = str(user_input.get(CONF_NAME, "")).strip()
            self._language = str(user_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE))

            if not raw_house:
                errors[CONF_HOUSE] = "invalid_house"
            else:
                entry = self._home_response.find_house(raw_house) if self._home_response else None
                self._house = entry.house_number if entry is not None else raw_house
                if entry is not None and len(entry.group_ids) == 1:
                    self._group = entry.group_ids[0]
                    self._manual = False
                    return await self._finish_entry()

                self._candidate_groups = list(entry.group_ids) if entry is not None else []
                self._manual = entry is None or len(entry.group_ids) == 0
                return await self.async_step_group()

        house_selector: Any
        if houses:
            sorted_houses = sorted(houses.keys(), key=natural_house_sort_key)
            house_options = [selector.SelectOptionDict(value=h, label=h) for h in sorted_houses]
            house_selector = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=house_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            )
        else:
            house_selector = selector.TextSelector()

        lang_options = [selector.SelectOptionDict(value=lang, label=lang) for lang in LANGUAGES]
        schema = vol.Schema(
            {
                vol.Required(CONF_HOUSE, default=self._house or vol.UNDEFINED): house_selector,
                vol.Optional(CONF_NAME, default=self._name): selector.TextSelector(),
                vol.Required(CONF_LANGUAGE, default=self._language): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=lang_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="language",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="house",
            data_schema=schema,
            errors=errors,
            description_placeholders={"settlement": self._settlement, "street": self._street},
        )

    async def async_step_group(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 5: Manual primary group selection when a house has 0 or >1 groups."""
        if user_input is not None:
            self._group = str(user_input[CONF_GROUP])
            if self._candidate_groups and self._group not in self._candidate_groups:
                self._manual = True
            return await self._finish_entry()

        sch_names = (
            self._home_response.preset.sch_names
            if self._home_response and self._home_response.preset
            else {}
        )
        preset_groups = (
            self._home_response.preset.available_groups
            if self._home_response and self._home_response.preset
            else []
        )

        ordered_groups: list[str] = []
        for gid in (*self._candidate_groups, *preset_groups, *DEFAULT_GROUPS):
            if gid not in ordered_groups:
                ordered_groups.append(gid)

        options = [
            selector.SelectOptionDict(value=gid, label=_format_group_label(gid, sch_names))
            for gid in ordered_groups
        ]
        default_group = self._group if self._group in ordered_groups else ordered_groups[0]
        schema = vol.Schema(
            {
                vol.Required(CONF_GROUP, default=default_group): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="group", data_schema=schema)

    async def async_step_group_only(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Alternative setup when monitoring only a group without street address."""
        self._mode = MODE_GROUP_ONLY
        if user_input is not None:
            self._group = str(user_input[CONF_GROUP])
            self._name = str(user_input.get(CONF_NAME, "")).strip()
            self._language = str(user_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE))
            self._manual = True
            return await self._finish_entry()

        options = [
            selector.SelectOptionDict(value=gid, label=_format_group_label(gid, {}))
            for gid in DEFAULT_GROUPS
        ]
        default_group = self._group if self._group in DEFAULT_GROUPS else DEFAULT_GROUPS[0]
        lang_options = [selector.SelectOptionDict(value=lang, label=lang) for lang in LANGUAGES]
        mirror_note = (
            "No GitHub mirror exists for Donetsk region; DTEK direct polling will be used."
            if MIRROR_URLS.get(self._site_key) is None
            else ""
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_GROUP, default=default_group): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_NAME, default=self._name): selector.TextSelector(),
                vol.Required(CONF_LANGUAGE, default=self._language): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=lang_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="language",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="group_only",
            data_schema=schema,
            description_placeholders={"mirror_note": mirror_note},
        )

    async def async_step_unreachable(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Fallback step when DTEK site is unreachable or blocked by WAF."""
        if user_input is not None:
            action = str(user_input["action"])
            if action == "retry":
                return await self._load_streets_and_advance()
            return await self.async_step_group_only()

        mirror_warning = (
            "Note: Donetsk region (dem) has no backup mirror; group-only mode still polls DTEK."
            if MIRROR_URLS.get(self._site_key) is None
            else ""
        )
        schema = vol.Schema(
            {
                vol.Required("action", default="retry"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["retry", MODE_GROUP_ONLY],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="unreachable_action",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="unreachable",
            data_schema=schema,
            errors={"base": self._unreachable_reason},
            description_placeholders={"mirror_warning": mirror_warning},
        )


class UaOutagesOptionsFlow(OptionsFlowWithReload):
    """Options flow allowing live tuning without hardcoding."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage polling, warning thresholds, mirror, multi-group mode, and language."""
        site_key = str(self.config_entry.data[CONF_SITE_KEY])
        mode = str(self.config_entry.data.get(CONF_MODE, MODE_ADDRESS))
        main_group = str(self.config_entry.data.get(CONF_GROUP, ""))
        has_mirror = MIRROR_URLS.get(site_key) is not None

        if user_input is not None:
            cleaned = dict(user_input)
            cleaned[CONF_UPDATE_INTERVAL] = int(user_input[CONF_UPDATE_INTERVAL])
            cleaned[CONF_WARN_BEFORE_START] = sorted(
                {int(v) for v in user_input.get(CONF_WARN_BEFORE_START, [])},
                reverse=True,
            )
            cleaned[CONF_WARN_BEFORE_END] = sorted(
                {int(v) for v in user_input.get(CONF_WARN_BEFORE_END, [])},
                reverse=True,
            )
            if not has_mirror:
                cleaned[CONF_USE_MIRROR] = False
            return self.async_create_entry(title="", data=cleaned)

        opts = self.config_entry.options
        warn_options = [
            selector.SelectOptionDict(value=str(m), label=f"{m} min") for m in WARN_MINUTE_OPTIONS
        ]
        start_defaults = [
            str(m) for m in opts.get(CONF_WARN_BEFORE_START, DEFAULT_WARN_BEFORE_START)
        ]
        end_defaults = [str(m) for m in opts.get(CONF_WARN_BEFORE_END, DEFAULT_WARN_BEFORE_END)]
        lang_options = [selector.SelectOptionDict(value=lang, label=lang) for lang in LANGUAGES]

        fields: dict[Any, Any] = {
            vol.Required(
                CONF_UPDATE_INTERVAL,
                default=int(opts.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_UPDATE_INTERVAL,
                    max=MAX_UPDATE_INTERVAL,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_WARN_BEFORE_START,
                default=start_defaults,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=warn_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(
                CONF_WARN_BEFORE_END,
                default=end_defaults,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=warn_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }

        if has_mirror:
            fields[
                vol.Required(
                    CONF_USE_MIRROR,
                    default=bool(opts.get(CONF_USE_MIRROR, DEFAULT_USE_MIRROR)),
                )
            ] = selector.BooleanSelector()

        fields[
            vol.Required(
                CONF_SHOW_POSSIBLE,
                default=bool(opts.get(CONF_SHOW_POSSIBLE, DEFAULT_SHOW_POSSIBLE)),
            )
        ] = selector.BooleanSelector()

        fields[
            vol.Required(
                CONF_GROUPS_MODE,
                default=str(opts.get(CONF_GROUPS_MODE, DEFAULT_GROUPS_MODE)),
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(GROUPS_MODES),
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="groups_mode",
            )
        )

        other_group_choices = [
            selector.SelectOptionDict(value=gid, label=gid.removeprefix("GPV"))
            for gid in DEFAULT_GROUPS
            if gid != main_group
        ]
        if mode == MODE_ADDRESS:
            fields[
                vol.Optional(
                    CONF_EXCLUDED_GROUPS,
                    default=list(opts.get(CONF_EXCLUDED_GROUPS, [])),
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=other_group_choices,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        else:
            fields[
                vol.Optional(
                    CONF_EXTRA_GROUPS,
                    default=list(opts.get(CONF_EXTRA_GROUPS, [])),
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=other_group_choices,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        fields[
            vol.Required(
                CONF_LANGUAGE,
                default=str(opts.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)),
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=lang_options,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="language",
            )
        )

        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))

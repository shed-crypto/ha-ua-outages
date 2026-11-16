"""Base entity class with fixed English object IDs and per-entry language names."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LANGUAGE, DEFAULT_LANGUAGE, DOMAIN, REGION_NAMES
from .coordinator import UaOutagesCoordinator

ENTITY_NAMES: dict[str, dict[str, str]] = {
    "uk": {
        "group": "Черга відключень",
        "next_outage": "Наступне відключення",
        "outage_end": "Кінець відключень",
        "outages_today": "Відключень сьогодні",
        "outages_tomorrow": "Відключень завтра",
        "groups_status": "Статус груп",
        "address_status": "Статус за адресою",
        "restoration": "Очікуване відновлення",
        "schedule_updated": "Графік оновлено",
        "last_update": "Останнє опитування",
        "connection": "Підключення",
        "scheduled_outage": "Планове відключення",
        "address_outage": "Відключення за адресою",
        "outages": "Графік відключень",
        "other_groups": "Інші групи",
        "events": "Події відключень",
    },
    "en": {
        "group": "Disconnection group",
        "next_outage": "Next outage",
        "outage_end": "Outage end",
        "outages_today": "Outages today",
        "outages_tomorrow": "Outages tomorrow",
        "groups_status": "Groups status",
        "address_status": "Address status",
        "restoration": "Expected restoration",
        "schedule_updated": "Schedule updated",
        "last_update": "Last poll",
        "connection": "Connection",
        "scheduled_outage": "Scheduled outage",
        "address_outage": "Address outage",
        "outages": "Outages",
        "other_groups": "Other groups",
        "events": "Outage events",
    },
    "ru": {
        "group": "Группа отключений",
        "next_outage": "Следующее отключение",
        "outage_end": "Конец отключения",
        "outages_today": "Отключений сегодня",
        "outages_tomorrow": "Отключений завтра",
        "groups_status": "Статус групп",
        "address_status": "Статус по адресу",
        "restoration": "Ожидаемое восстановление",
        "schedule_updated": "График обновлен",
        "last_update": "Последний опрос",
        "connection": "Подключение",
        "scheduled_outage": "Плановое отключение",
        "address_outage": "Отключение по адресу",
        "outages": "График отключений",
        "other_groups": "Другие группы",
        "events": "События отключений",
    },
}

CALENDAR_LABELS: dict[str, dict[str, str]] = {
    "uk": {
        "definite_outage": "Відключення електроенергії",
        "possible_outage": "Можливе відключення",
        "group_prefix": "Черга",
        "emergency": "Аварійне відключення",
        "planned": "Планові роботи",
        "other": "Відключення за адресою",
    },
    "en": {
        "definite_outage": "Power outage",
        "possible_outage": "Possible outage",
        "group_prefix": "Group",
        "emergency": "Emergency outage",
        "planned": "Planned works",
        "other": "Address outage",
    },
    "ru": {
        "definite_outage": "Отключение электроэнергии",
        "possible_outage": "Возможное отключение",
        "group_prefix": "Группа",
        "emergency": "Аварийное отключение",
        "planned": "Плановые работы",
        "other": "Отключение по адресу",
    },
}


class UaOutagesEntity(CoordinatorEntity[UaOutagesCoordinator]):
    """Common base entity for Ukraine Power Outages."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UaOutagesCoordinator, entity_key: str) -> None:
        super().__init__(coordinator)
        self._entity_key = entity_key
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{entity_key}"
        self._attr_translation_key = entity_key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=coordinator.config_entry.title,
            manufacturer="DTEK / UA Outages",
            model=REGION_NAMES.get(coordinator.site_key, coordinator.site_key),
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def language(self) -> str:
        """Return configured entity language independently of HA UI locale."""
        raw = str(self.coordinator.config_entry.options.get(CONF_LANGUAGE, DEFAULT_LANGUAGE))
        return raw if raw in ENTITY_NAMES else DEFAULT_LANGUAGE

    @property
    def name(self) -> str | None:
        """Return localized display name according to entry language option."""
        lang_map = ENTITY_NAMES.get(self.language, ENTITY_NAMES[DEFAULT_LANGUAGE])
        return lang_map.get(self._entity_key, self._entity_key)

    @property
    def suggested_object_id(self) -> str | None:
        """Keep English entity_id suffixes stable regardless of configured UI language."""
        return self._entity_key

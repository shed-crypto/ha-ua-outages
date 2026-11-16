# Ukraine Power Outages (`ua_outages`)

Multi-region Home Assistant custom integration for monitoring planned, probable, and emergency electricity outage schedules in Ukraine, built on [`dtek-blackout-client`](https://github.com/shed-crypto/dtek-blackout-client).

## Features

- **5 DTEK regions supported out of the box:** `kem` (Kyiv city), `krem` (Kyiv oblast), `dnem` (Dnipropetrovsk oblast), `dem` (Donetsk oblast), and `oem` (Odesa oblast)
- **Address-based or group-only monitoring:** full street/building lookup (including emergency banners and external operator detection) or standalone group monitoring
- **Blackout-resilient `.storage` cache:** survives Home Assistant restarts without internet and continues operating seamlessly with `data_status: stale`
- **Automatic GitHub mirror fallback:** uses `Baskerville42/outage-data-ua` when DTEK is blocked or unreachable (`kem`, `krem`, `dnem`, `oem`; `dem` operates directly + `.storage`)
- **DST-safe schedule math:** 48 half-hour slots per day mapped into UTC intervals with spring 23h and autumn 25h (`fold=0`/`fold=1`) transitions and midnight merging
- **Exact local boundary timers:** fires state updates and warning events at exact slot boundaries, configurable warning thresholds, and local midnight without polling DTEK
- **Per-entry entity language (`uk` / `en` / `ru`):** switches display names independently of HA UI locale while keeping English `entity_id` suffixes stable
- **16 entities, 10 events, 2 services, and redacted diagnostics**

## Supported Regions

| `site_key` | Operator | Region | Mirror Fallback |
| :--- | :--- | :--- | :--- |
| `kem` | DTEK Kyiv City Networks | Kyiv city | Yes |
| `krem` | DTEK Kyiv Regional Networks | Kyiv oblast | Yes |
| `dnem` | DTEK Dnipro Networks | Dnipropetrovsk oblast | Yes |
| `dem` | DTEK Donetsk Networks | Donetsk oblast | Direct + `.storage` only |
| `oem` | DTEK Odesa Networks | Odesa oblast | Yes |

## Installation

### HACS (Custom Repository)

1. Open **HACS** -> **Integrations** -> **Custom repositories**.
2. Add `https://github.com/shed-crypto/ha-ua-outages` with category **Integration**.
3. Install **Ukraine Power Outages** and restart Home Assistant (`2025.1.0`+).
4. Add the integration under **Settings** -> **Devices & services** -> **Add integration**.

### Manual Installation

Copy `custom_components/ua_outages` into `<config>/custom_components/ua_outages` and restart Home Assistant.

## Entities & Services

- **Sensors (`11`):** `group`, `next_outage`, `outage_end`, `outages_today`, `outages_tomorrow`, `groups_status`, `address_status`, `restoration`, `schedule_updated`, `last_update`, `connection`
- **Binary sensors (`2`):** `scheduled_outage`, `address_outage`
- **Calendars (`2`):** `outages`, `other_groups`
- **Event entity (`1`) + `ua_outages_event` bus events (`10`):** `schedule_published`, `schedule_changed`, `outage_warning`, `possible_outage_warning`, `outage_end_warning`, `outage_started`, `outage_ended`, `address_outage`, `group_changed`, `groups_changed`
- **Services (`2`):** `ua_outages.refresh`, `ua_outages.get_schedule` (`SupportsResponse.ONLY`)

## Example Automations (Read-Only Telemetry)

> When integrating with a solar/battery inverter (such as Deye + LiFePO4), keep inverter telemetry **read-only** (`SOC`, load power, voltage) and trigger alerts or smart-plug load shedding externally.

### Telegram Pre-Outage Warning

```yaml
alias: Outage warning with battery runtime check
triggers:
  - trigger: event
    event_type: ua_outages_event
    event_data:
      event_type: outage_warning
actions:
  - action: notify.telegram
    data:
      message: >-
        Power outage starts in {{ trigger.event.data.minutes }} min
        for {{ (trigger.event.data.duration_minutes / 60) | round(1) }} h.
        Current battery SOC: {{ states('sensor.deye_battery_soc') }}%.
```

### Tomorrow Schedule Published Summary

```yaml
alias: Notify when tomorrow outage schedule is published
triggers:
  - trigger: event
    event_type: ua_outages_event
    event_data:
      event_type: schedule_published
actions:
  - action: notify.telegram
    data:
      message: >-
        Schedule published for {{ trigger.event.data.date }}:
        {{ states('sensor.home_outages_tomorrow') }} h
        ({{ state_attr('sensor.home_outages_tomorrow', 'outages_text') }}).
```

## Development

```bash
pip install -r requirements_test.txt
ruff check custom_components/ tests/
black --check custom_components/ tests/
mypy custom_components/ua_outages/
pytest
```

## License

[MIT](LICENSE) 2026 Rachenko Oleksii

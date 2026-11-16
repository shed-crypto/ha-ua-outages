# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `force_weekly_preset` option in Options Flow allowing `calendar.*_outages` and `calendar.*_other_groups` to render baseline weekly preset possible-outage intervals even when DTEK sets `weekly_shown=False`.

## [0.1.0] - 2026-11-16

### Added
- Multi-region support (`kem`, `krem`, `dnem`, `dem`, `oem`) powered by `dtek-blackout-client==0.2.1`.
- 48-slot schedule engine with DST-safe UTC intervals and midnight merging.
- Persistent `.storage` cache, `Baskerville42/outage-data-ua` mirror fallback, exponential backoff, group stabilization, and exact boundary timers.
- Multi-step `ConfigFlow` (with settlement auto-skip for single-city regions, tolerant search, reconfigure) and `OptionsFlowWithReload`.
- 16 Home Assistant entities (`sensor`, `binary_sensor`, `calendar`, `event`), 10 event types, `ua_outages.refresh` and `ua_outages.get_schedule` services, redacted diagnostics, and `en`/`uk`/`ru` translations.

# Ukraine Power Outages (`ua_outages`)

Multi-region Home Assistant custom integration for monitoring planned and emergency electricity outage schedules in Ukraine, built on top of [`dtek-blackout-client`](https://github.com/shed-crypto/dtek-blackout-client).

> **Status:** Early development (Stage 1 repository scaffold). Implementation is progressing stage-by-stage per the architecture specification.

## Supported Regions (DTEK)

| `site_key` | Operator | Region | Mirror Fallback |
| :--- | :--- | :--- | :--- |
| `kem` | DTEK Kyiv City Networks | Kyiv city | Yes |
| `krem` | DTEK Kyiv Regional Networks | Kyiv oblast | Yes |
| `dnem` | DTEK Dnipro Networks | Dnipropetrovsk oblast | Yes |
| `dem` | DTEK Donetsk Networks | Donetsk oblast | Direct + local storage only |
| `oem` | DTEK Odesa Networks | Odesa oblast | Yes |

## Requirements

- Home Assistant (`2025.1.0`+ / Python `3.12`+)
- `dtek-blackout-client==0.2.1`

## Development

```bash
pip install -r requirements_test.txt
ruff check custom_components/ tests/
black --check custom_components/ tests/
mypy custom_components/ua_outages/
pytest
```

## License

[MIT](LICENSE)


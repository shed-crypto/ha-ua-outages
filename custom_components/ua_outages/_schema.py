"""Schema library selector supporting Voluptuous and Probatio."""

from __future__ import annotations

from importlib import import_module
from typing import Any


def _load_schema_module() -> Any:
    """Return probatio when available, otherwise fall back to voluptuous."""
    try:
        return import_module("probatio")
    except ImportError:
        return import_module("voluptuous")


vol: Any = _load_schema_module()

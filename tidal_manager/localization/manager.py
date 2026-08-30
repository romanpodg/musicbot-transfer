"""Load, validate, and format localized CLI messages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class LocalizationError(RuntimeError):
    """Raised when a localization catalog is malformed or incomplete."""


class LocalizationManager:
    """A small JSON-backed localization service with a safe English fallback."""

    def __init__(self, directory: Path, language: str = "en") -> None:
        self._directory = directory
        self._catalogs = self._load_catalogs()
        self._language = language if language in self._catalogs else "en"

    @property
    def language(self) -> str:
        """Return the active ISO language code."""

        return self._language

    def set_language(self, language: str) -> None:
        """Select an installed language catalog."""

        if language not in self._catalogs:
            raise LocalizationError("language_not_available")
        self._language = language

    def available_languages(self) -> dict[str, str]:
        """Return language codes mapped to their self-described names."""

        return {
            language: str(catalog["language_name"])
            for language, catalog in self._catalogs.items()
        }

    def t(self, key: str, **values: object) -> str:
        """Translate a dotted key and safely interpolate named values."""

        try:
            message = _lookup(self._catalogs[self._language], key)
        except KeyError:
            message = _lookup(self._catalogs["en"], key)
        if not isinstance(message, str):
            raise LocalizationError("message_not_string")
        try:
            return message.format(**values)
        except (KeyError, ValueError) as error:
            raise LocalizationError("message_format_invalid") from error

    def validate_catalogs(self) -> None:
        """Verify all installed catalogs expose the same user-facing keys."""

        expected = _leaf_keys(self._catalogs["en"])
        for language, catalog in self._catalogs.items():
            if _leaf_keys(catalog) != expected:
                raise LocalizationError(f"catalog_keys_mismatch:{language}")

    def _load_catalogs(self) -> dict[str, dict[str, Any]]:
        catalogs: dict[str, dict[str, Any]] = {}
        for path in self._directory.glob("*.json"):
            with path.open("r", encoding="utf-8") as handle:
                catalog = json.load(handle)
            if not isinstance(catalog, dict) or not isinstance(catalog.get("language_name"), str):
                raise LocalizationError("catalog_invalid")
            catalogs[path.stem] = catalog
        if "en" not in catalogs:
            raise LocalizationError("english_catalog_missing")
        return catalogs


def _lookup(catalog: dict[str, Any], key: str) -> Any:
    """Resolve a dotted translation key."""

    value: Any = catalog
    for part in key.split("."):
        if not isinstance(value, dict):
            raise KeyError(key)
        value = value[part]
    return value


def _leaf_keys(value: dict[str, Any], prefix: str = "") -> set[str]:
    """Return every dotted key holding a translatable string."""

    keys: set[str] = set()
    for key, item in value.items():
        current = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            keys.update(_leaf_keys(item, current))
        elif isinstance(item, str):
            keys.add(current)
        else:
            raise LocalizationError("catalog_value_invalid")
    return keys

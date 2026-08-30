"""Load, validate, and format localized interface messages.

The core never produces user-facing text: it raises exceptions carrying a stable
``code`` (see :mod:`music_transfer.core.errors`).  Interfaces translate those
codes here, so the same error renders in English or Russian without the core
knowing that languages exist.

Catalogs are JSON files under ``<locale>/<name>.json``.  English is mandatory and
acts as the fallback for missing keys.  Every catalog must expose exactly the
same set of leaf keys; :meth:`LocalizationManager.validate_catalogs` enforces
that, and a test covers it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Directory holding one subdirectory per language.
LOCALES_DIRECTORY = Path(__file__).resolve().parent

#: The catalog that must always exist and that backs missing-key fallbacks.
DEFAULT_LANGUAGE = "en"


class LocalizationError(RuntimeError):
    """Raised when a localization catalog is malformed or incomplete."""


class LocalizationManager:
    """A small JSON-backed localization service with a safe English fallback."""

    def __init__(self, directory: Path | str | None = None, language: str = DEFAULT_LANGUAGE) -> None:
        self._directory = Path(directory) if directory else LOCALES_DIRECTORY
        self._catalogs = _load_catalogs(self._directory)
        self._language = language if language in self._catalogs else DEFAULT_LANGUAGE

    @property
    def directory(self) -> Path:
        """Return the directory catalogs are loaded from."""

        return self._directory

    @property
    def language(self) -> str:
        """Return the active ISO language code."""

        return self._language

    def set_language(self, language: str) -> None:
        """Select an installed language catalog.

        Raises:
            LocalizationError: If the language is not installed.
        """

        if language not in self._catalogs:
            raise LocalizationError("language_not_available")
        self._language = language

    def available_languages(self) -> dict[str, str]:
        """Return language codes mapped to their self-described names."""

        return {
            language: str(catalog["language_name"])
            for language, catalog in sorted(self._catalogs.items())
        }

    def t(self, key: str, **values: object) -> str:
        """Translate a dotted key and safely interpolate named values.

        Falls back to the English catalog when the active language lacks the
        key, so an incomplete translation degrades instead of crashing.

        Raises:
            LocalizationError: If the key is missing from every catalog, or the
                message cannot be formatted with the supplied values.
        """

        try:
            message = _lookup(self._catalogs[self._language], key)
        except KeyError:
            self._log_missing_key(key)
            try:
                message = _lookup(self._catalogs[DEFAULT_LANGUAGE], key)
            except KeyError as error:
                raise LocalizationError(f"message_key_missing:{key}") from error
        if not isinstance(message, str):
            raise LocalizationError("message_not_string")
        try:
            return message.format(**values)
        except (KeyError, ValueError, IndexError) as error:
            raise LocalizationError("message_format_invalid") from error

    def error(self, code: str, **values: object) -> str:
        """Translate a stable core error code into a localized message.

        Unknown codes are rendered with the generic ``errors.unknown`` template
        rather than raising, because an untranslated new error code must never
        hide the original failure from the user.
        """

        fallback = self._catalogs[self._language].get("errors")
        english = self._catalogs[DEFAULT_LANGUAGE].get("errors") or {}
        for catalog in (fallback, english):
            if isinstance(catalog, dict) and code in catalog:
                try:
                    return str(catalog[code]).format(**values)
                except (KeyError, ValueError, IndexError):
                    break
        return str(english.get("unknown", "⚠️ {code}")).format(code=code)

    def has_key(self, key: str) -> bool:
        """Return whether ``key`` resolves in the active or English catalog."""

        for catalog in (self._catalogs[self._language], self._catalogs[DEFAULT_LANGUAGE]):
            try:
                value = _lookup(catalog, key)
            except KeyError:
                continue
            return isinstance(value, str)
        return False

    def validate_catalogs(self) -> None:
        """Verify all installed catalogs expose the same user-facing keys.

        Raises:
            LocalizationError: On any structural mismatch or invalid value.
        """

        expected = _leaf_keys(self._catalogs[DEFAULT_LANGUAGE])
        for language, catalog in sorted(self._catalogs.items()):
            if _leaf_keys(catalog) != expected:
                missing = sorted(expected - _leaf_keys(catalog))
                extra = sorted(_leaf_keys(catalog) - expected)
                raise LocalizationError(
                    f"catalog_keys_mismatch:{language}:missing={missing}:extra={extra}"
                )

    def _log_missing_key(self, key: str) -> None:
        """Record a fallback without raising (a partial catalog is usable)."""

        if self._language == DEFAULT_LANGUAGE:
            return
        import logging

        logging.getLogger("music_transfer.localization").warning(
            "event=translation_missing language=%s key=%s", self._language, key
        )


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


def _load_catalogs(directory: Path) -> dict[str, dict[str, Any]]:
    """Read every language catalog, requiring English to be present."""

    catalogs: dict[str, dict[str, Any]] = {}
    for language_directory in sorted(directory.iterdir()):
        if not language_directory.is_dir():
            continue
        for path in sorted(language_directory.glob("*.json")):
            try:
                catalog = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise LocalizationError(f"catalog_unreadable:{path.name}") from error
            if not isinstance(catalog, dict) or not isinstance(
                catalog.get("language_name"), str
            ):
                raise LocalizationError("catalog_invalid")
            # A language may be split across several files (messages, errors).
            merged = catalogs.setdefault(language_directory.name, {})
            _deep_merge(merged, catalog)
    if DEFAULT_LANGUAGE not in catalogs:
        raise LocalizationError("english_catalog_missing")
    return catalogs


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> None:
    """Merge ``extra`` into ``base`` recursively."""

    for key, value in extra.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(current, value)
        else:
            base[key] = value


__all__ = ["DEFAULT_LANGUAGE", "LocalizationError", "LocalizationManager"]

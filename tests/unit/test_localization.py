"""Localization: catalog parity, safe fallback, and error-code coverage.

The core never produces user-facing text - it raises exceptions carrying a
stable ``code``.  These tests guard the contract that makes that safe:

* every shipped catalog exposes exactly the same keys;
* a partial catalog degrades to English instead of crashing;
* an unknown error code still renders something readable;
* every error code the source can raise actually has a translation.

The last check walks the source with :mod:`ast` rather than reading a
hand-maintained list, so a new ``raise SomeError("new_code")`` that nobody
translated fails the suite instead of reaching a user as a raw code.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from music_transfer import __file__ as _PACKAGE_INIT
from music_transfer.core import errors
from music_transfer.core.errors import MusicTransferError
from music_transfer.locales.manager import (
    DEFAULT_LANGUAGE,
    LocalizationError,
    LocalizationManager,
    _leaf_keys,
)

PACKAGE_ROOT = Path(_PACKAGE_INIT).resolve().parent

#: Where error codes can originate.  ``core`` raises them; ``platforms``
#: translates SDK failures into them.
_SOURCE_ROOTS = (PACKAGE_ROOT / "core", PACKAGE_ROOT / "platforms")

_ERROR_CLASS_NAMES: frozenset[str] = frozenset(
    name
    for name, member in vars(errors).items()
    if isinstance(member, type) and issubclass(member, MusicTransferError)
)


def _call_name(node: ast.expr) -> str | None:
    """Return the bare name of a call target, or ``None`` if it has none."""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def raised_error_codes() -> set[str]:
    """Return every error-code literal the source can raise.

    Recognises ``raise SomeError("code")`` for any class derived from
    :class:`MusicTransferError`, plus local helper names ending in ``Error``
    (``_not_found`` returns ``NotFoundError("destination_identifier_missing")``
    through a helper, which the plain-name rule would miss).
    """

    codes: set[str] = set()
    for root in _SOURCE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                name = _call_name(node.func)
                if name is None:
                    continue
                if name not in _ERROR_CLASS_NAMES and not name.endswith("Error"):
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    codes.add(first.value)
    return codes


class CatalogParity(unittest.TestCase):
    """Every language exposes the same keys."""

    def setUp(self) -> None:
        """Load the shipped catalogs."""

        self.manager = LocalizationManager()

    def test_validate_catalogs_passes(self) -> None:
        """The shipped catalogs are structurally identical."""

        self.manager.validate_catalogs()

    def test_english_and_russian_have_identical_keys(self) -> None:
        """No translation is missing and none is orphaned."""

        reference = _leaf_keys(_catalog_of(self.manager, DEFAULT_LANGUAGE))
        for language in self.manager.available_languages():
            keys = _leaf_keys(_catalog_of(self.manager, language))
            self.assertEqual(keys, reference, f"catalog drift in {language!r}")

    def test_every_language_can_be_selected(self) -> None:
        """Each installed catalog is loadable on its own."""

        for language in self.manager.available_languages():
            LocalizationManager(language=language).validate_catalogs()

    def test_unknown_language_is_rejected(self) -> None:
        """Selecting an uninstalled language fails loudly."""

        with self.assertRaises(LocalizationError):
            self.manager.set_language("xx")

    def test_missing_language_falls_back_to_english(self) -> None:
        """An unknown language code degrades instead of crashing."""

        manager = LocalizationManager(language="xx")
        self.assertEqual(manager.language, DEFAULT_LANGUAGE)


class TranslationBehaviour(unittest.TestCase):
    """Fallbacks degrade; they never crash or hide a failure."""

    def setUp(self) -> None:
        """Load the shipped catalogs."""

        self.manager = LocalizationManager()

    def test_has_key_matches_lookup(self) -> None:
        """``has_key`` agrees with ``t`` about what is translatable."""

        self.assertTrue(self.manager.has_key("language_name"))
        self.assertFalse(self.manager.has_key("does.not.exist"))

    def test_unknown_message_key_raises(self) -> None:
        """A missing message key is a programming error, so it raises."""

        with self.assertRaises(LocalizationError):
            self.manager.t("does.not.exist")

    def test_interpolation_substitutes_values(self) -> None:
        """Named placeholders are substituted."""

        rendered = self.manager.t("language_name")
        self.assertNotIn("{", rendered)

    def test_unknown_error_code_still_renders(self) -> None:
        """An untranslated code shows the code, never a blank message."""

        for language in self.manager.available_languages():
            manager = LocalizationManager(language=language)
            rendered = manager.error("this_code_does_not_exist_yet")
            self.assertIn("this_code_does_not_exist_yet", rendered)

    def test_known_error_code_renders_in_every_language(self) -> None:
        """A known code produces real text, not the generic template."""

        for language in self.manager.available_languages():
            manager = LocalizationManager(language=language)
            rendered = manager.error("same_account_transfer")
            self.assertNotIn("same_account_transfer", rendered)
            self.assertTrue(rendered.strip())


class ErrorCodeCoverage(unittest.TestCase):
    """Every code the source can raise has a translation."""

    def setUp(self) -> None:
        """Load the shipped catalogs."""

        self.manager = LocalizationManager()

    def test_discovery_finds_codes(self) -> None:
        """The AST scan is not vacuously passing on an empty set."""

        self.assertGreater(len(raised_error_codes()), 10)

    def test_every_raised_code_is_translated(self) -> None:
        """No error code can reach a user untranslated."""

        codes = raised_error_codes()
        for language in self.manager.available_languages():
            manager = LocalizationManager(language=language)
            untranslated = sorted(
                code for code in codes if not manager.has_key(f"errors.{code}")
            )
            self.assertEqual(
                untranslated, [], f"missing {language} translations: {untranslated}"
            )


def _catalog_of(manager: LocalizationManager, language: str) -> dict:
    """Return one language's merged catalog (test helper)."""

    from music_transfer.locales.manager import _load_catalogs

    return _load_catalogs(manager.directory)[language]


if __name__ == "__main__":
    unittest.main()

"""Localization catalogs and the manager that serves them.

Only interface code imports this package.  The core raises errors carrying
stable codes and stays language-agnostic.
"""

from .manager import (
    DEFAULT_LANGUAGE,
    LOCALES_DIRECTORY,
    LocalizationError,
    LocalizationManager,
)

__all__ = [
    "DEFAULT_LANGUAGE",
    "LOCALES_DIRECTORY",
    "LocalizationError",
    "LocalizationManager",
]

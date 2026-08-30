"""The original ``tidal_manager`` test suite, preserved unchanged.

These tests import the legacy package as top-level modules (``core.auth``,
``ui.menu``, ...), so the legacy project root is added to ``sys.path`` here
before any test module is imported.  That keeps the historical regression suite
running byte-for-byte while the new architecture takes over.

If the legacy package is ever removed, delete this directory - the new suite
under ``tests/unit`` covers the same invariants.
"""

from __future__ import annotations

import sys
from pathlib import Path

LEGACY_ROOT = Path(__file__).resolve().parents[2] / "tidal_manager"

if str(LEGACY_ROOT) not in sys.path:
    sys.path.insert(0, str(LEGACY_ROOT))

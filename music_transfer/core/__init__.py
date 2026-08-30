"""The platform-independent core.

Layers depend inward: ``interfaces -> app -> core`` and
``platforms -> core.ports``.  Nothing in this package may import aiogram,
FastAPI, Redis, or a music-platform SDK.
"""

from __future__ import annotations

__all__ = ["domain", "enums", "errors", "matching", "ports", "transfer"]

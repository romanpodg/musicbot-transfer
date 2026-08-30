"""Platform adapters.

Each adapter implements ``music_transfer.core.ports.MusicPlatformAdapter`` and
translates provider errors into the core taxonomy at its boundary.
"""

from __future__ import annotations

from .registry import PlatformRegistry, default_registry, unimplemented

__all__ = ["PlatformRegistry", "default_registry", "unimplemented"]

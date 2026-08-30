"""music_transfer - a platform-independent music migration core.

Package layout (dependencies point inward)::

    interfaces  ->  app  ->  core
    platforms   ->  core.ports
    infrastructure -> core.ports

``core`` must never import aiogram, FastAPI, Redis, or a music-platform SDK.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]

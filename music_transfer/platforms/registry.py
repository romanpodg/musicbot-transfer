"""The platform adapter registry.

This is the **only** place in the codebase where a platform name selects an
implementation.  Everything else asks for capabilities.  Adding a platform
means registering a factory here; no core module changes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..core.enums import Platform
from ..core.errors import UnsupportedCapabilityError
from ..core.ports import MusicPlatformAdapter, PlatformCapabilities

_LOGGER = logging.getLogger("music_transfer.platforms.registry")

#: Signature every adapter factory must have.  ``client`` is the platform's own
#: authenticated client object; the registry does not know its type.
AdapterFactory = Callable[..., MusicPlatformAdapter]


class PlatformRegistry:
    """Map platform identifiers onto adapter factories."""

    def __init__(self) -> None:
        self._factories: dict[Platform, AdapterFactory] = {}

    def register(self, platform: Platform, factory: AdapterFactory) -> None:
        """Register an adapter factory for a platform."""

        self._factories[platform] = factory
        _LOGGER.info("event=platform_registered platform=%s", platform.value)

    def create(self, platform: Platform, *args: Any, **kwargs: Any) -> MusicPlatformAdapter:
        """Create an adapter for a platform.

        Raises:
            UnsupportedCapabilityError: If no adapter is registered.  A missing
                platform is reported the same way as a missing capability so a
                caller has one error path to handle.
        """

        factory = self._factories.get(platform)
        if factory is None:
            raise UnsupportedCapabilityError(
                "platform_not_registered", capability=f"platform:{platform.value}"
            )
        return factory(*args, **kwargs)

    def registered(self) -> tuple[Platform, ...]:
        """Return every registered platform."""

        return tuple(self._factories)

    def capabilities_for(self, platform: Platform) -> PlatformCapabilities | None:
        """Return a platform's declared capabilities without authenticating."""

        return _STATIC_CAPABILITIES.get(platform)


#: Capability declarations available without a session.  Kept in sync with the
#: adapters so a future UI can render options before a user connects.
_STATIC_CAPABILITIES: dict[Platform, PlatformCapabilities] = {}


def _register_builtin_platforms(registry: PlatformRegistry) -> None:
    """Register the adapters shipped with this release."""

    from .tidal.adapter import TidalAdapter, build_tidal_adapter

    registry.register(Platform.TIDAL, build_tidal_adapter)
    _STATIC_CAPABILITIES[Platform.TIDAL] = TidalAdapter.CAPABILITIES


def default_registry() -> PlatformRegistry:
    """Return a registry with every built-in adapter registered."""

    registry = PlatformRegistry()
    _register_builtin_platforms(registry)
    return registry


def unimplemented(platform: Platform) -> AdapterFactory:
    """Return a factory that always refuses, for unshipped platforms.

    Registering an explicit refusal is better than leaving a platform absent:
    the failure names the platform and says it is unimplemented, instead of
    looking like a registry bug.  It never pretends an operation succeeded.
    """

    def factory(*args: Any, **kwargs: Any) -> MusicPlatformAdapter:
        raise UnsupportedCapabilityError(
            "platform_not_implemented", capability=f"platform:{platform.value}"
        )

    return factory

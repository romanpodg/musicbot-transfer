"""Ports (interfaces) that the core depends on.

Adapters implement ``platform``; infrastructure implements ``repositories``.
The core never imports the implementations.
"""

from __future__ import annotations

from .platform import (
    KNOWN_DESTINATION_SECTIONS,
    AsyncPlatformAdapter,
    DestinationState,
    LibraryMaintenanceAdapter,
    MusicPlatformAdapter,
    MusicPlatformReadPort,
    PlatformCapabilities,
    ReadOnlyAdapter,
    destination_section_for_entity,
    operation_kind,
    to_async,
)
from .queue import InlineQueue, JobQueue, QueueMessage
from .repositories import (
    AccountRepository,
    TransferItemRepository,
    TransferJobRepository,
    TransferPlanRepository,
    UnitOfWork,
)

__all__ = [
    "AccountRepository",
    "AsyncPlatformAdapter",
    "DestinationState",
    "InlineQueue",
    "JobQueue",
    "KNOWN_DESTINATION_SECTIONS",
    "LibraryMaintenanceAdapter",
    "MusicPlatformAdapter",
    "MusicPlatformReadPort",
    "PlatformCapabilities",
    "QueueMessage",
    "ReadOnlyAdapter",
    "TransferItemRepository",
    "TransferJobRepository",
    "TransferPlanRepository",
    "UnitOfWork",
    "destination_section_for_entity",
    "operation_kind",
    "to_async",
]

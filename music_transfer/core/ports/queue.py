"""Queue port for out-of-band transfer execution.

Nothing here is implemented yet.  The port exists so that the application
service can be written today against an interface that a Redis/Celery/Dramatiq
worker can satisfy later, without adding that infrastructure now.

The intended shape::

    Telegram / API  ->  create TransferJob  ->  queue.enqueue(job_id)
                                                      |
                                                   worker
                                                      v
                                            Transfer Application Service

The local CLI executes jobs synchronously through the same application
service, so both paths share all business logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class QueueMessage:
    """A unit of work handed to a worker."""

    job_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {"job_id": self.job_id, "payload": dict(self.payload), "attempts": self.attempts}


class JobQueue(ABC):
    """Enqueue and dequeue transfer work."""

    @abstractmethod
    def enqueue(self, message: QueueMessage) -> None:
        """Schedule a job for execution."""

    @abstractmethod
    def dequeue(self, *, timeout_seconds: float = 0.0) -> QueueMessage | None:
        """Take the next message, or ``None`` when the queue is empty."""

    @abstractmethod
    def ack(self, message: QueueMessage) -> None:
        """Mark a message as fully processed."""


class InlineQueue(JobQueue):
    """A synchronous in-process queue used by the CLI.

    This is not a fake: it really does execute in order, in memory.  It is the
    correct implementation for a single-process CLI and a useful test double.
    """

    def __init__(self) -> None:
        self._pending: list[QueueMessage] = []
        self._acked: list[QueueMessage] = []

    def enqueue(self, message: QueueMessage) -> None:
        self._pending.append(message)

    def dequeue(self, *, timeout_seconds: float = 0.0) -> QueueMessage | None:
        return self._pending.pop(0) if self._pending else None

    def ack(self, message: QueueMessage) -> None:
        self._acked.append(message)

    @property
    def pending(self) -> list[QueueMessage]:
        """Return messages waiting to be processed."""

        return list(self._pending)

    @property
    def acked(self) -> list[QueueMessage]:
        """Return messages that were acknowledged."""

        return list(self._acked)

"""Recovery: resuming interrupted work without repeating confirmed operations.

Resume is deliberately **not** modelled as ``last_position = 613``.  After a
crash the destination may hold a different number of objects than a local
counter suggests (a write can land and its acknowledgement can be lost), so
per-item status is the only trustworthy record.

The service answers three questions:

1. Which items still need work?          -> :meth:`pending_items`
2. Which items may a retry job include?  -> :meth:`select_for_retry`
3. Can an ambiguous item be resolved?    -> :meth:`resolve_ambiguous`
"""

from __future__ import annotations

import logging
from typing import Any

from ..domain import TransferItem, TransferJob
from ..enums import (
    RETRYABLE_ITEM_STATUSES,
    TERMINAL_ITEM_STATUSES,
    DestinationPresence,
    ItemStatus,
    JobStatus,
    MutationState,
)
from ..errors import InvalidDestinationSectionError
from ..ports import DestinationState, TransferItemRepository

_LOGGER = logging.getLogger("music_transfer.recovery")


class RecoveryService:
    """Derive safe resume and retry plans from persisted item state."""

    def __init__(
        self,
        items: TransferItemRepository,
        logger: logging.Logger | None = None,
    ) -> None:
        self._items = items
        self._logger = logger or _LOGGER

    def pending_items(self, job_id: str) -> list[TransferItem]:
        """Return the items a resumed run must still process.

        Terminal items (transferred, already present, skipped) are excluded so
        that replaying a job cannot duplicate confirmed work (Invariant E).
        """

        stored = self._items.list_for_job(job_id)
        pending = [item for item in stored if not item.is_terminal()]
        self._logger.info(
            "event=recovery_pending job_id=%s total=%d pending=%d",
            job_id,
            len(stored),
            len(pending),
        )
        return pending

    def select_for_retry(
        self, job_id: str, statuses: tuple[ItemStatus, ...] | None = None
    ) -> list[TransferItem]:
        """Return the items a retry job may include.

        Args:
            job_id: The original job.
            statuses: Which statuses to retry.  Defaults to every retryable
                status except ``PENDING``, because pending items belong to the
                original run.

        Raises:
            ValueError: If a caller asks for a terminal status, which would
                duplicate confirmed work.
        """

        wanted = statuses or tuple(
            status for status in RETRYABLE_ITEM_STATUSES if status is not ItemStatus.PENDING
        )
        unsafe = set(wanted) & TERMINAL_ITEM_STATUSES
        if unsafe:
            raise ValueError(f"retry_status_not_retryable:{sorted(status.value for status in unsafe)}")
        stored = self._items.list_for_job(job_id)
        return [
            item
            for item in stored
            if item.status in wanted and item.mutation_state is not MutationState.IN_FLIGHT
        ]

    def counters(self, job_id: str) -> dict[str, int]:
        """Return per-status counts, always including every known status."""

        counts = {status.value: 0 for status in ItemStatus}
        counts.update(self._items.count_by_status(job_id))
        return counts

    def resumable(self, job: TransferJob) -> bool:
        """Return whether a job has work left and is not terminal."""

        if job.is_finished:
            return False
        return bool(self.pending_items(job.id))

    def resolve_ambiguous(
        self,
        job_id: str,
        state: DestinationState,
        *,
        entity_type_matches: bool = True,
    ) -> list[TransferItem]:
        """Turn ambiguous items into confirmed ones using destination state.

        An ambiguous item means "we do not know whether the write landed".
        Re-reading the destination resolves it safely: if the identifier is
        present, the write succeeded and must never be repeated.

        Returns the items whose status changed. Items that are ABSENT or UNKNOWN
        stay ambiguous so they are never blindly auto-replayed.
        """

        del entity_type_matches  # Reserved for per-type resolution policies.
        resolved: list[TransferItem] = []
        for item in self._items.list_for_job(job_id):
            if item.status is not ItemStatus.AMBIGUOUS:
                continue
            identifier = item.destination_id
            if not identifier:
                continue
            try:
                presence = state.presence(item.entity_type, identifier)
            except InvalidDestinationSectionError:
                continue

            if presence is DestinationPresence.PRESENT:
                item.mark(ItemStatus.TRANSFERRED, error=None)
                item.last_failure_kind = None
                self._items.update(item)
                resolved.append(item)
                self._logger.info(
                    "event=ambiguity_resolved job_id=%s item_id=%s source_id=%s presence=present",
                    job_id,
                    item.id,
                    item.source_id,
                )
            elif presence is DestinationPresence.ABSENT:
                self._logger.info(
                    "event=ambiguity_unresolved job_id=%s item_id=%s source_id=%s presence=absent",
                    job_id,
                    item.id,
                    item.source_id,
                )
            elif presence is DestinationPresence.UNKNOWN:
                self._logger.info(
                    "event=ambiguity_unresolved job_id=%s item_id=%s source_id=%s presence=unknown",
                    job_id,
                    item.id,
                    item.source_id,
                )
        return resolved

    def snapshot(self, job: TransferJob) -> dict[str, Any]:
        """Return a compact, loggable summary of a job's recovery state."""

        counters = self.counters(job.id)
        return {
            "job_id": job.id,
            "status": str(job.status),
            "cancellation_requested": job.cancellation_requested,
            "counts": counters,
            "pending": counters.get(ItemStatus.PENDING.value, 0)
            + counters.get(ItemStatus.MATCHED.value, 0),
        }


def job_status_for_recovery(job: TransferJob) -> JobStatus:
    """Return the status a job should carry while awaiting recovery."""

    return JobStatus.PAUSED if not job.is_finished else job.status



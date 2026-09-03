"""Transfer execution: item-level writes with resume, idempotency, and cancellation.

Design rules encoded here:

* **Resume is item-level** (Invariant E).  Terminal items are never replayed;
  the engine reads statuses from the repository rather than trusting a single
  position counter.
* **Ambiguous outcomes are never auto-replayed** (Invariant F).  A timeout is
  not proof that a write failed, so an ambiguous result is recorded and left
  for reconciliation via destination state.
* **Cancellation is cooperative.**  The executor finishes the current item,
  persists it, and stops.  Already transferred content is never rolled back
  automatically.
* **Failure meanings stay explicit.**  ``NOT_FOUND``, ``UNAVAILABLE``,
  ``AMBIGUOUS``, and ``FAILED`` are distinct so a retry job can select exactly
  the items worth retrying.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..domain import (
    Playlist,
    TransferItem,
    TransferJob,
    TransferProgress,
    TransferReport,
)
from ..enums import EntityType, ItemStatus, MutationState, TransferOperation
from ..errors import (
    AmbiguousOperationError,
    AuthenticationError,
    AuthorizationError,
    MusicTransferError,
    TemporaryPlatformError,
    UnsupportedCapabilityError,
    classify_error,
)
from ..ports import MusicPlatformAdapter, TransferItemRepository
from .lifecycle import status_after_execution

__all__ = [
    "CancellationToken",
    "ExecutionOutcome",
    "ExecutionResult",
    "TransferExecutor",
    "build_report",
    "scrub_credentials",
    "status_after_execution",
]

_LOGGER = logging.getLogger("music_transfer.executor")

ProgressCallback = Callable[[TransferProgress], None]


class CancellationToken:
    """Cooperative cancellation flag shared with the driving interface.

    A UI (CLI ``Ctrl+C``, a future Telegram "Cancel" button) sets this.  The
    executor checks it between items, so stopping happens at a safe boundary
    instead of killing a request mid-flight.
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation."""

        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        """Return whether cancellation was requested."""

        return self._cancelled


#: How each failure kind maps onto a durable item status.
_FAILURE_STATUS: dict[str, ItemStatus] = {
    "ambiguous": ItemStatus.AMBIGUOUS,
    "unavailable": ItemStatus.UNAVAILABLE,
    "not_found": ItemStatus.NOT_FOUND,
    "authentication": ItemStatus.FAILED,
    "temporary": ItemStatus.FAILED,
    "permanent": ItemStatus.FAILED,
}


_CREDENTIAL_PATTERNS = (
    re.compile(
        r"(?i)(access[_ -]?token|refresh[_ -]?token|authorization|password|api[_ -]?key|secret)"
        r"\s*[=:]\s*(?:bearer\s+)?[^\s,;]+"
    ),
    re.compile(r"(?i)bearer\s+[^\s,;]+"),
    re.compile(r"(?i)\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.?[A-Za-z0-9_\-]*"),
)


def scrub_credentials(text: str | None) -> str | None:
    """Redact token-like or credential-like values from an error message."""
    if not text:
        return text
    sanitized = text
    for pattern in _CREDENTIAL_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


_scrub_credentials = scrub_credentials


@dataclass(slots=True)
class ExecutionResult:
    """Authoritative structured outcome of an execution run."""

    processed: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: bool = False
    aborted: bool = False
    abort_error: str | None = None
    abort_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def aborted_error(self) -> str | None:
        """Backward-compatible alias for abort_error."""
        return self.abort_error

    @aborted_error.setter
    def aborted_error(self, value: str | None) -> None:
        self.abort_error = value
        if value is not None:
            self.aborted = True

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""
        return {
            "processed": self.processed,
            "succeeded": self.succeeded,
            "skipped": self.skipped,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "aborted": self.aborted,
            "abort_error": self.abort_error,
            "abort_reason": self.abort_reason,
            "warnings": list(self.warnings),
            "aborted_error": self.abort_error,
        }


ExecutionOutcome = ExecutionResult


class TransferExecutor:
    """Write planned items to a destination, checkpointing after every item."""

    def __init__(
        self,
        destination: MusicPlatformAdapter,
        items: TransferItemRepository,
        *,
        logger: logging.Logger | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        self._destination = destination
        self._items = items
        self._logger = logger or _LOGGER
        self._cancellation = cancellation_token or CancellationToken()

    @property
    def cancellation_token(self) -> CancellationToken:
        """Return the token a caller can use to stop the run."""

        return self._cancellation

    def execute(
        self,
        job: TransferJob,
        plan_items: list[TransferItem],
        *,
        progress: ProgressCallback | None = None,
    ) -> ExecutionOutcome:
        """Execute planned items in order.

        Args:
            job: The job being executed (counters are updated in place).
            plan_items: Items in the order they should be written.
            progress: Optional progress sink.

        Returns:
            An :class:`ExecutionOutcome`.  Item statuses are already persisted.
        """

        from .planner import validate_plan_write_positions

        validate_plan_write_positions(plan_items)

        outcome = ExecutionResult()
        ordered = self._write_order(job, plan_items)
        total = len(ordered)
        blocked_containers: set[str] = set()
        for _, item in enumerate(ordered, start=1):
            if self._cancellation.is_cancelled or job.cancellation_requested:
                outcome.cancelled = True
                self._logger.warning(
                    "event=execution_cancelled job_id=%s processed=%d", job.id, outcome.processed
                )
                break
            if item.is_terminal():
                outcome.skipped += 1
                outcome.processed += 1
                self._emit(progress, job, "importing", total, outcome, item)
                continue
            if not item.is_executable():
                # Item cannot be executed by destination mutation (e.g. AMBIGUOUS,
                # NOT_FOUND, UNAVAILABLE, or missing operation/destination).
                outcome.skipped += 1
                outcome.processed += 1
                self._emit(progress, job, "importing", total, outcome, item)
                continue
            if self._attempts_exhausted(job, item):
                outcome.skipped += 1
                outcome.processed += 1
                self._emit(progress, job, "importing", total, outcome, item)
                continue

            c_keys = self._item_container_keys(item)
            if c_keys and any(k in blocked_containers for k in c_keys):
                err_name = (
                    "playlist_sequence_blocked"
                    if item.entity_type is EntityType.PLAYLIST_ITEM
                    else "container_blocked"
                )
                self._logger.warning(
                    "event=%s job_id=%s container_id=%s item_id=%s write_position=%s recovery_decision=blocked",
                    err_name,
                    job.id,
                    c_keys[0],
                    item.id,
                    item.write_position,
                )
                item.last_failure_kind = "ambiguous"
                item.mark(ItemStatus.AMBIGUOUS, error=err_name)
                self._items.update(item)
                if item.entity_type in (EntityType.FOLDER, EntityType.PLAYLIST):
                    blocked_containers.add(f"src:{item.source_id}")
                    if item.destination_id:
                        blocked_containers.add(f"dst:{item.destination_id}")
                outcome.failed += 1
                outcome.processed += 1
                self._emit(progress, job, "importing", total, outcome, item)
                continue

            try:
                self._execute_item(job, item, ordered)
            except (AuthenticationError, AuthorizationError) as error:
                # Losing the session makes every remaining item meaningless.
                self._record_failure(item, error)
                self._items.update(item)
                outcome.failed += 1
                outcome.processed += 1
                outcome.aborted = True
                outcome.abort_error = getattr(error, "code", "authentication_error")
                raw_reason = getattr(error, "message", None) or str(error)
                outcome.abort_reason = _scrub_credentials(raw_reason)
                self._logger.error(
                    "event=execution_aborted job_id=%s error=%s reason=%s",
                    job.id,
                    outcome.abort_error,
                    outcome.abort_reason,
                )
                break

            if item.status in (ItemStatus.FAILED, ItemStatus.AMBIGUOUS):
                if item.entity_type in (EntityType.FOLDER, EntityType.PLAYLIST):
                    blocked_containers.add(f"src:{item.source_id}")
                    if item.destination_id:
                        blocked_containers.add(f"dst:{item.destination_id}")
                elif item.entity_type is EntityType.PLAYLIST_ITEM and c_keys:
                    for k in c_keys:
                        blocked_containers.add(k)

            outcome.processed += 1
            if item.status is ItemStatus.TRANSFERRED:
                outcome.succeeded += 1
            elif item.status is ItemStatus.SKIPPED or item.status is ItemStatus.ALREADY_EXISTS:
                outcome.skipped += 1
            elif item.status is not ItemStatus.MATCHED:
                outcome.failed += 1
            self._emit(progress, job, "importing", total, outcome, item)
        job.processed_items = outcome.processed
        job.successful_items = outcome.succeeded
        job.failed_items = outcome.failed
        job.skipped_items = outcome.skipped
        job.touch()
        return outcome

    # -- item execution ----------------------------------------------------

    def _execute_item(
        self, job: TransferJob, item: TransferItem, all_items: list[TransferItem]
    ) -> None:
        """Write one item and persist the result.

        Args:
            job: The owning job.
            item: The item to write.
            all_items: Every planned item, used to propagate a newly created
                playlist identifier to its entries.
        """

        if job.settings.dry_run:
            item.mark(ItemStatus.SKIPPED, error="dry_run")
            self._items.update(item)
            return
        item.register_attempt()
        try:
            op = item.operation
            if op is TransferOperation.SAVE_TRACK or (op is TransferOperation.NONE and item.entity_type is EntityType.TRACK):
                self._write_track(item)
            elif op is TransferOperation.SAVE_ALBUM or (op is TransferOperation.NONE and item.entity_type is EntityType.ALBUM):
                self._write_album(item)
            elif op is TransferOperation.FOLLOW_ARTIST or (op is TransferOperation.NONE and item.entity_type is EntityType.ARTIST):
                self._write_artist(item)
            elif op is TransferOperation.SAVE_VIDEO or (op is TransferOperation.NONE and item.entity_type is EntityType.VIDEO):
                self._write_video(item)
            elif op is TransferOperation.SAVE_MIX or (op is TransferOperation.NONE and item.entity_type is EntityType.MIX):
                self._write_mix(item)
            elif op is TransferOperation.CREATE_PLAYLIST or (op is TransferOperation.NONE and item.entity_type is EntityType.PLAYLIST):
                self._write_playlist(job, item, all_items)
            elif op is TransferOperation.ADD_PLAYLIST_ITEM or (op is TransferOperation.NONE and item.entity_type is EntityType.PLAYLIST_ITEM):
                self._write_playlist_item(item, all_items)
            elif op is TransferOperation.CREATE_FOLDER or (op is TransferOperation.NONE and item.entity_type is EntityType.FOLDER):
                self._write_folder(job, item, all_items)
            else:
                item.mark(ItemStatus.SKIPPED, error="operation_not_executable")
                self._items.update(item)
                return
        except (AuthenticationError, AuthorizationError):
            raise
        except MusicTransferError as error:
            self._record_failure(item, error)
            self._items.update(item)
            self._logger.error(
                "event=item_failed job_id=%s item_id=%s entity=%s source_id=%s reason=%s attempt=%d",
                job.id,
                item.id,
                item.entity_type.value,
                item.source_id,
                getattr(error, "code", "unknown"),
                item.attempt_count,
            )
            return
        except Exception as error:  # noqa: BLE001 - one bad item must not abort a library
            # An exception the adapter did not translate (an SDK bug, an
            # unexpected payload) is classified here, recorded as a durable
            # item failure, and chained into the log.  It is never swallowed:
            # the item stays FAILED and retryable, and the job continues.
            self._record_unexpected_failure(item, error)
            self._items.update(item)
            self._logger.error(
                "event=item_failed job_id=%s item_id=%s entity=%s source_id=%s reason=%s attempt=%d",
                job.id,
                item.id,
                item.entity_type.value,
                item.source_id,
                type(error).__name__,
                item.attempt_count,
                exc_info=error,
            )
            return
        item.last_failure_kind = None
        item.last_error = None
        item.mutation_state = MutationState.NONE
        item.mark(ItemStatus.TRANSFERRED)
        self._items.update(item)
        self._logger.info(
            "event=item_transferred job_id=%s item_id=%s entity=%s source_id=%s destination_id=%s",
            job.id,
            item.id,
            item.entity_type.value,
            item.source_id,
            item.destination_id or "",
        )

    def _write_track(self, item: TransferItem) -> None:
        if not item.destination_id:
            raise _not_found(item)
        self._destination.save_track(item.destination_id)

    def _write_album(self, item: TransferItem) -> None:
        if not item.destination_id:
            raise _not_found(item)
        self._destination.save_album(item.destination_id)

    def _write_artist(self, item: TransferItem) -> None:
        if not item.destination_id:
            raise _not_found(item)
        self._destination.follow_artist(item.destination_id)

    def _write_video(self, item: TransferItem) -> None:
        if not item.destination_id:
            raise _not_found(item)
        self._destination.save_video(item.destination_id)

    def _write_mix(self, item: TransferItem) -> None:
        if not item.destination_id:
            raise _not_found(item)
        self._destination.save_mix(item.destination_id)

    def _write_playlist(
        self, job: TransferJob, item: TransferItem, all_items: list[TransferItem]
    ) -> None:
        """Create a destination playlist exactly once, then record its id.

        The identifier is written onto the item *and* onto every child entry so
        that a resume can find it without re-creating the playlist.
        """

        if item.destination_id:
            self._propagate_container(all_items, item.source_id, item.destination_id)
            return

        target_folder_id: str | None = None
        if item.container_source_id is not None:
            target_folder_id = item.container_destination_id
            if not target_folder_id:
                parent_folder = next(
                    (
                        i
                        for i in all_items
                        if i.entity_type is EntityType.FOLDER
                        and i.source_id == item.container_source_id
                    ),
                    None,
                )
                if parent_folder and parent_folder.destination_id:
                    target_folder_id = parent_folder.destination_id
                    item.container_destination_id = target_folder_id
            if not target_folder_id:
                raise AmbiguousOperationError("playlist_parent_folder_destination_id_missing")

        playlist = Playlist(
            source_platform=job.source_platform,
            source_id=item.source_id,
            name=str(item.source_metadata.get("name", "")),
            description=item.source_metadata.get("description") or "",
            folder_id=target_folder_id,
        )
        created_id = self._destination.create_playlist(playlist)
        if not created_id:
            raise AmbiguousOperationError("playlist_creation_unconfirmed")
        item.destination_id = created_id
        item.match_score = 1.0
        self._propagate_container(all_items, item.source_id, created_id)
        self._logger.info(
            "event=playlist_created job_id=%s source_id=%s destination_id=%s folder_id=%s",
            job.id,
            item.source_id,
            created_id,
            target_folder_id,
        )

    def _write_folder(
        self, job: TransferJob, item: TransferItem, all_items: list[TransferItem]
    ) -> None:
        """Create a destination playlist folder exactly once, then record its id."""

        if item.destination_id:
            self._propagate_folder_container(all_items, item.source_id, item.destination_id)
            return

        parent_destination_id: str | None = None
        if item.container_source_id is not None:
            parent_destination_id = item.container_destination_id
            if not parent_destination_id:
                parent_folder = next(
                    (
                        i
                        for i in all_items
                        if i.entity_type is EntityType.FOLDER
                        and i.source_id == item.container_source_id
                    ),
                    None,
                )
                if parent_folder and parent_folder.destination_id:
                    parent_destination_id = parent_folder.destination_id
                    item.container_destination_id = parent_destination_id
            if not parent_destination_id:
                raise AmbiguousOperationError("parent_folder_destination_id_missing")

        folder_name = str(
            item.source_metadata.get("name")
            or item.source_metadata.get("title")
            or item.source_id
        )

        if item.mutation_state is MutationState.IN_FLIGHT:
            self._reconcile_folder(item, folder_name, parent_destination_id)
            if item.status is ItemStatus.TRANSFERRED and item.destination_id:
                self._propagate_folder_container(all_items, item.source_id, item.destination_id)
                return

        item.mutation_state = MutationState.IN_FLIGHT
        self._items.update(item)

        try:
            created_id = self._destination.create_folder(folder_name, parent_destination_id)
            if not created_id:
                raise AmbiguousOperationError("folder_creation_unconfirmed")
            item.destination_id = created_id
            item.match_score = 1.0
            item.mutation_state = MutationState.NONE
            self._propagate_folder_container(all_items, item.source_id, created_id)
            self._logger.info(
                "event=folder_created job_id=%s source_id=%s destination_id=%s parent_destination_id=%s",
                job.id,
                item.source_id,
                created_id,
                parent_destination_id,
            )
        except (TemporaryPlatformError, AmbiguousOperationError) as error:
            self._reconcile_folder(item, folder_name, parent_destination_id)
            if item.status is ItemStatus.TRANSFERRED and item.destination_id:
                self._propagate_folder_container(all_items, item.source_id, item.destination_id)
                return
            raise AmbiguousOperationError("folder_creation_ambiguous") from error

    def _reconcile_folder(
        self,
        item: TransferItem,
        folder_name: str,
        parent_destination_id: str | None,
    ) -> None:
        """Attempt to reconcile an in-flight or failed folder creation against destination state."""

        try:
            snapshot = self._destination.export_library(sections=("folders",))
        except Exception as error:  # noqa: BLE001
            self._logger.warning(
                "event=folder_reconciliation_failed job_id=%s item_id=%s error=%s",
                item.job_id,
                item.id,
                error,
            )
            item.last_failure_kind = "ambiguous"
            item.mark(ItemStatus.AMBIGUOUS, error="folder_reconciliation_inconclusive")
            self._items.update(item)
            return

        if "folders" in snapshot.incomplete_sections:
            item.last_failure_kind = "ambiguous"
            item.mark(ItemStatus.AMBIGUOUS, error="folder_reconciliation_inconclusive")
            self._items.update(item)
            return

        from .planner import folder_parent_source_id

        norm_expected_parent = (
            None
            if parent_destination_id in (None, "root", "")
            else str(parent_destination_id)
        )

        matching = [
            f
            for f in snapshot.folders
            if f.title == folder_name
            and (
                None
                if folder_parent_source_id(f) in (None, "root", "")
                else str(folder_parent_source_id(f))
            )
            == norm_expected_parent
        ]

        if len(matching) == 1:
            item.destination_id = matching[0].source_id
            item.match_score = 1.0
            item.mutation_state = MutationState.NONE
            item.last_failure_kind = None
            item.last_error = None
            item.mark(ItemStatus.TRANSFERRED)
            self._items.update(item)
            self._logger.info(
                "event=folder_reconciled job_id=%s item_id=%s destination_id=%s",
                item.job_id,
                item.id,
                item.destination_id,
            )
        else:
            item.last_failure_kind = "ambiguous"
            item.mark(ItemStatus.AMBIGUOUS, error="folder_creation_ambiguous")
            self._items.update(item)

    @staticmethod
    def _propagate_folder_container(
        all_items: list[TransferItem], container_source_id: str, destination_id: str
    ) -> None:
        """Record a newly created folder container ID on all child folders and playlists."""

        for entry in all_items:
            if (
                entry.entity_type in (EntityType.FOLDER, EntityType.PLAYLIST)
                and entry.container_source_id == container_source_id
            ):
                entry.container_destination_id = destination_id

    def _write_playlist_item(self, item: TransferItem, all_items: list[TransferItem]) -> None:
        """Append one playlist entry, reconciling first when the API allows it."""

        container_id = item.container_destination_id or self._find_container(all_items, item)
        if not container_id:
            raise _not_found(item)
        if not item.destination_id:
            raise _not_found(item)
        self._reconcile(item, all_items, container_id)
        if item.status is ItemStatus.TRANSFERRED:
            return

        # Write ordering invariant: persist mutation intent = IN_FLIGHT BEFORE remote call.
        # If persisting IN_FLIGHT fails, the exception propagates and add_playlist_item is never called.
        item.mutation_state = MutationState.IN_FLIGHT
        try:
            self._items.update(item)
        except Exception:
            item.mutation_state = MutationState.NONE
            raise

        try:
            self._destination.add_playlist_item(container_id, item.destination_id)
        except (AmbiguousOperationError, TemporaryPlatformError) as error:
            try:
                actual = list(self._destination.playlist_item_ids(container_id))
            except (AuthenticationError, AuthorizationError):
                raise
            except UnsupportedCapabilityError:
                item.last_failure_kind = "ambiguous"
                item.mark(ItemStatus.AMBIGUOUS, error="playlist_commit_unverifiable")
                self._items.update(item)
                raise AmbiguousOperationError("playlist_commit_unverifiable") from error
            except Exception as inspect_err:
                # If checking destination state also errors, mark ambiguous and raise AmbiguousOperationError
                item.last_failure_kind = "ambiguous"
                item.mark(ItemStatus.AMBIGUOUS, error="playlist_state_inconclusive")
                self._items.update(item)
                raise AmbiguousOperationError("playlist_state_inconclusive") from inspect_err

            expected = [
                entry.destination_id
                for entry in self._siblings(all_items, item.container_source_id)
                if entry.write_position is not None and entry.destination_id
            ]

            if item.write_position is not None:
                # Validate the prefix before accepting absence or presence
                if actual[: item.write_position] != expected[: item.write_position]:
                    item.last_failure_kind = "ambiguous"
                    item.mark(ItemStatus.AMBIGUOUS, error="playlist_resume_mismatch")
                    self._items.update(item)
                    raise AmbiguousOperationError("playlist_resume_mismatch") from error

                if (
                    len(actual) > item.write_position
                    and actual[item.write_position] == item.destination_id
                ):
                    # The mutation actually committed remotely despite timeout/error!
                    item.mutation_state = MutationState.NONE
                    item.mark(ItemStatus.TRANSFERRED)
                    self._items.update(item)
                    return
                elif (
                    len(actual) == item.write_position
                    and actual == expected[: item.write_position]
                ):
                    # Confirmed absence: remote state did not receive the item.
                    # Clear in-flight mutation state so item can be retried safely.
                    item.mutation_state = MutationState.NONE
                    self._items.update(item)
                    raise error

            # Inconclusive or diverged: fail safely without blind duplicates.
            item.last_failure_kind = "ambiguous"
            item.mark(ItemStatus.AMBIGUOUS, error="playlist_state_inconclusive")
            self._items.update(item)
            raise AmbiguousOperationError("playlist_state_inconclusive") from error

    # -- resume helpers ----------------------------------------------------

    def _reconcile(
        self, item: TransferItem, all_items: list[TransferItem], container_id: str
    ) -> None:
        """Skip an entry the destination already contains after a crash.

        Reads the destination's exact media sequence and compares it against
        the planned prefix.  A mismatch means the destination content is not
        what this plan expects, so the item is marked ambiguous rather than
        blindly appended (which would corrupt ordering).
        """

        if item.write_position is None:
            return
        try:
            actual = list(self._destination.playlist_item_ids(container_id))
        except (AuthenticationError, AuthorizationError):
            raise
        except UnsupportedCapabilityError as error:
            if item.mutation_state is MutationState.IN_FLIGHT:
                item.last_failure_kind = "ambiguous"
                item.mark(ItemStatus.AMBIGUOUS, error="playlist_commit_unverifiable")
                self._items.update(item)
                raise AmbiguousOperationError("playlist_commit_unverifiable") from error
            return
        except Exception as error:
            item.last_failure_kind = "ambiguous"
            item.mark(ItemStatus.AMBIGUOUS, error="playlist_state_inconclusive")
            self._items.update(item)
            raise AmbiguousOperationError("playlist_state_inconclusive") from error

        expected = [
            entry.destination_id
            for entry in self._siblings(all_items, item.container_source_id)
            if entry.write_position is not None and entry.destination_id
        ]

        if len(actual) < item.write_position:
            self._logger.warning(
                "event=playlist_predecessor_missing item_id=%s write_position=%d actual_len=%d",
                item.id,
                item.write_position,
                len(actual),
            )
            item.last_failure_kind = "ambiguous"
            item.mark(ItemStatus.AMBIGUOUS, error="playlist_predecessor_missing")
            self._items.update(item)
            raise AmbiguousOperationError("playlist_predecessor_missing")

        if len(actual) > len(expected) or actual != expected[: len(actual)]:
            item.last_failure_kind = "ambiguous"
            item.mark(ItemStatus.AMBIGUOUS, error="playlist_resume_mismatch")
            self._items.update(item)
            raise AmbiguousOperationError("playlist_resume_mismatch")

        if len(actual) > item.write_position and actual[item.write_position] == item.destination_id:
            # The write landed before the checkpoint was saved.
            item.mutation_state = MutationState.NONE
            item.mark(ItemStatus.TRANSFERRED)
            self._items.update(item)
            return

        if (
            len(actual) == item.write_position
            and actual == expected[: item.write_position]
            and item.mutation_state is MutationState.IN_FLIGHT
        ):
            # Confirmed absence: previous attempt did not commit. Clear stale intent.
            self._logger.info(
                "event=inflight_confirmed_absent item_id=%s write_position=%d",
                item.id,
                item.write_position,
            )
            item.mutation_state = MutationState.NONE
            self._items.update(item)

    @staticmethod
    def _siblings(all_items: list[TransferItem], container_source_id: str | None) -> list[TransferItem]:
        """Return the planned entries of one playlist, in write order."""

        if container_source_id is None:
            return []
        siblings = [
            entry
            for entry in all_items
            if entry.entity_type is EntityType.PLAYLIST_ITEM
            and entry.container_source_id == container_source_id
        ]
        return sorted(
            siblings,
            key=lambda entry: (
                entry.write_position is None,
                entry.write_position if entry.write_position is not None else entry.original_position,
            ),
        )

    @staticmethod
    def _propagate_container(
        all_items: list[TransferItem], container_source_id: str, destination_id: str
    ) -> None:
        """Record a newly created container id on all of its entries."""

        for entry in all_items:
            if (
                entry.entity_type is EntityType.PLAYLIST_ITEM
                and entry.container_source_id == container_source_id
            ):
                entry.container_destination_id = destination_id

    @staticmethod
    def _find_container(all_items: list[TransferItem], item: TransferItem) -> str | None:
        """Return the destination container id recorded by the playlist item."""

        for entry in all_items:
            if (
                entry.entity_type is EntityType.PLAYLIST
                and entry.source_id == item.container_source_id
                and entry.destination_id
            ):
                return entry.destination_id
        return None

    @staticmethod
    def _item_container_keys(item: TransferItem) -> list[str]:
        """Return container identification keys that this item depends on."""

        keys: list[str] = []
        if item.container_source_id:
            keys.append(f"src:{item.container_source_id}")
        if item.container_destination_id:
            keys.append(f"dst:{item.container_destination_id}")
        return keys

    # -- bookkeeping -------------------------------------------------------

    def _write_order(self, job: TransferJob, items: list[TransferItem]) -> list[TransferItem]:
        """Return items in write order across container tiers.

        Tiers:
        1. Non-containers (tracks, albums, artists, videos, mixes)
        2. Root folders and child folders (topological order, parent before child)
        3. Playlists
        4. Playlist items (immediately following their playlist, preserving sequence)
        """

        from .ordering import restore_positions

        others = [
            item
            for item in items
            if item.entity_type not in (EntityType.FOLDER, EntityType.PLAYLIST, EntityType.PLAYLIST_ITEM)
        ]

        folder_items = [item for item in items if item.entity_type is EntityType.FOLDER]
        folder_by_src = {f.source_id: f for f in folder_items}
        folder_indices = {f.id: idx for idx, f in enumerate(folder_items)}
        depths: dict[str, int] = {}

        def get_fld_depth(src_id: str) -> int:
            if src_id in depths:
                return depths[src_id]
            f_item = folder_by_src.get(src_id)
            if (
                f_item is None
                or f_item.container_source_id is None
                or f_item.container_source_id not in folder_by_src
            ):
                depth = 0
            else:
                depth = get_fld_depth(f_item.container_source_id) + 1
            depths[src_id] = depth
            return depth

        sorted_folders = sorted(
            folder_items,
            key=lambda f: (get_fld_depth(f.source_id), folder_indices[f.id]),
        )

        playlists = [item for item in items if item.entity_type is EntityType.PLAYLIST]
        grouped: list[TransferItem] = list(others)
        grouped.extend(sorted_folders)
        for playlist in playlists:
            grouped.append(playlist)
            grouped.extend(
                restore_positions(
                    [
                        entry
                        for entry in items
                        if entry.entity_type is EntityType.PLAYLIST_ITEM
                        and entry.container_source_id == playlist.source_id
                    ],
                    position=lambda entry: (
                        entry.write_position
                        if entry.write_position is not None
                        else entry.original_position
                    ),
                )
            )
        return grouped

    @staticmethod
    def _attempts_exhausted(job: TransferJob, item: TransferItem) -> bool:
        """Return whether an item has already used all of its attempts."""

        return (
            item.status is ItemStatus.FAILED
            and item.attempt_count >= job.settings.max_item_attempts
        )

    @staticmethod
    def _record_failure(item: TransferItem, error: Exception) -> None:
        """Translate an exception into a durable item status."""

        kind = classify_error(error)
        status = _FAILURE_STATUS.get(kind, ItemStatus.FAILED)
        code = getattr(error, "code", None) or type(error).__name__
        item.last_failure_kind = kind
        item.mark(status, error=str(code))

    @staticmethod
    def _record_unexpected_failure(item: TransferItem, error: Exception) -> None:
        """Record an unclassified exception without losing its meaning.

        The failure kind is derived from the exception type where possible, so a
        network error raised by an SDK still becomes a retryable failure rather
        than a permanent one.  The exception type is kept in ``last_error`` so
        the log and the report stay diagnosable.
        """

        kind = classify_error(error)
        item.last_failure_kind = kind
        item.mark(
            _FAILURE_STATUS.get(kind, ItemStatus.FAILED),
            error=f"unexpected:{type(error).__name__}",
        )

    def _emit(
        self,
        progress: ProgressCallback | None,
        job: TransferJob,
        phase: str,
        total: int,
        outcome: ExecutionOutcome,
        item: TransferItem | None,
    ) -> None:
        """Publish a progress snapshot, if anyone is listening."""

        if progress is None:
            return
        progress(
            TransferProgress(
                job_id=job.id,
                phase=phase,
                total=total,
                processed=outcome.processed,
                succeeded=outcome.succeeded,
                skipped=outcome.skipped,
                failed=outcome.failed,
                current_item=_item_label(item),
            )
        )


def _item_label(item: TransferItem | None) -> str | None:
    """Return a short display label for a transfer item."""

    if item is None:
        return None
    title = item.source_metadata.get("title")
    return str(title) if title else item.source_id


def _not_found(item: TransferItem) -> MusicTransferError:
    """Return the error used when an item has no destination identifier."""

    from ..errors import NotFoundError

    return NotFoundError("destination_identifier_missing")


def build_report(
    job: TransferJob, items: list[TransferItem], outcome: ExecutionResult
) -> TransferReport:
    """Build a report from durable item state, not from transient counters."""

    report = TransferReport.from_items(
        job.id,
        items,
        operation="transfer",
        verification_status=job.verification_status,
    )
    if outcome.cancelled:
        report.warnings.append("cancelled")
    abort_err = outcome.abort_error or outcome.aborted_error
    if outcome.aborted or abort_err:
        report.warnings.append(f"aborted:{abort_err or 'unknown'}")
    return report

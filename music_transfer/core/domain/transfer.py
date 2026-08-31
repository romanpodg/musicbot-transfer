"""Transfer jobs, items, plans, progress, and reports.

These models are platform-independent and interface-independent.  Nothing here
knows about Telegram, TIDAL, JSON files, or Postgres.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..enums import (
    ContentType,
    EntityType,
    ItemStatus,
    JobStatus,
    MatchMethod,
    MutationState,
    OrderingMode,
    Platform,
    TransferOperation,
    VerificationStatus,
)
from ..errors import InvalidPersistedStateError


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""

    return datetime.now(UTC).isoformat()


def new_identifier(prefix: str) -> str:
    """Return a short, sortable, collision-resistant identifier."""

    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransferSettings:
    """User-selected behaviour that must stay stable across a resume.

    Attributes:
        ordering: The requested *logical* order of written items.
        preserve_visible_order: When ``True`` the executor compensates for a
            destination that shows the newest write first (see
            :mod:`music_transfer.core.transfer.ordering`).  It defaults to
            ``False`` so the proven legacy insertion order is preserved unless
            a caller explicitly asks for order compensation.
        allow_explicit_to_clean_fallback: Whether an explicit track may be
            replaced by a clean one.  Always ``False`` in this phase; the flag
            exists so the matcher never hard-codes the decision.
        allow_duplicates_in_playlists: Playlist duplicates are always
            preserved (Invariant D).  The flag is exposed for reporting only.
        skip_already_existing: Treat set-like content (liked tracks) as a set
            and skip items that already exist at the destination.
        max_item_attempts: Per-item attempt ceiling across resumes.
        dry_run: Never perform a write when set.
    """

    ordering: OrderingMode = OrderingMode.SOURCE_ORDER
    preserve_visible_order: bool = False
    allow_explicit_to_clean_fallback: bool = False
    allow_duplicates_in_playlists: bool = True
    skip_already_existing: bool = True
    max_item_attempts: int = 3
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "ordering": str(self.ordering),
            "preserve_visible_order": self.preserve_visible_order,
            "allow_explicit_to_clean_fallback": self.allow_explicit_to_clean_fallback,
            "allow_duplicates_in_playlists": self.allow_duplicates_in_playlists,
            "skip_already_existing": self.skip_already_existing,
            "max_item_attempts": self.max_item_attempts,
            "dry_run": self.dry_run,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> TransferSettings:
        """Rebuild settings, tolerating unknown or missing keys."""

        if not isinstance(value, dict):
            return cls()
        try:
            ordering = OrderingMode(str(value.get("ordering", OrderingMode.SOURCE_ORDER)))
        except ValueError:
            ordering = OrderingMode.SOURCE_ORDER
        return cls(
            ordering=ordering,
            preserve_visible_order=bool(value.get("preserve_visible_order", False)),
            allow_explicit_to_clean_fallback=bool(
                value.get("allow_explicit_to_clean_fallback", False)
            ),
            allow_duplicates_in_playlists=bool(
                value.get("allow_duplicates_in_playlists", True)
            ),
            skip_already_existing=bool(value.get("skip_already_existing", True)),
            max_item_attempts=max(1, int(value.get("max_item_attempts", 3))),
            dry_run=bool(value.get("dry_run", False)),
        )


# --------------------------------------------------------------------------
# Job
# --------------------------------------------------------------------------


@dataclass(slots=True)
class TransferJob:
    """A platform-independent, interface-independent transfer job.

    ``user_id`` is an opaque string owned by the calling interface (a Telegram
    user id, a CLI profile name, or ``None``).  The core never interprets it.
    """

    id: str
    source_platform: Platform
    destination_platform: Platform
    user_id: str | None = None
    source_account_id: str | None = None
    destination_account_id: str | None = None
    source_account_label: str | None = None
    destination_account_label: str | None = None
    requested_content: tuple[ContentType, ...] = (ContentType.LIKED_TRACKS,)
    settings: TransferSettings = field(default_factory=TransferSettings)
    status: JobStatus = JobStatus.CREATED
    verification_status: VerificationStatus = VerificationStatus.NOT_RUN
    cancellation_requested: bool = False
    error_code: str | None = None
    total_items: int = 0
    processed_items: int = 0
    successful_items: int = 0
    failed_items: int = 0
    skipped_items: int = 0
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        source_platform: Platform,
        destination_platform: Platform,
        *,
        user_id: str | None = None,
        requested_content: tuple[ContentType, ...] = (ContentType.LIKED_TRACKS,),
        settings: TransferSettings | None = None,
        source_account_id: str | None = None,
        destination_account_id: str | None = None,
    ) -> TransferJob:
        """Create a new job in the ``CREATED`` state with a fresh identifier."""

        return cls(
            id=new_identifier("job"),
            source_platform=source_platform,
            destination_platform=destination_platform,
            user_id=user_id,
            requested_content=requested_content,
            settings=settings or TransferSettings(),
            source_account_id=source_account_id,
            destination_account_id=destination_account_id,
        )

    def touch(self) -> None:
        """Mark the job as modified.  Callers persist immediately afterwards."""

        self.updated_at = utc_now()

    @property
    def is_finished(self) -> bool:
        """Return whether the job has reached a terminal state."""

        return self.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "id": self.id,
            "user_id": self.user_id,
            "source_platform": str(self.source_platform),
            "destination_platform": str(self.destination_platform),
            "source_account_id": self.source_account_id,
            "destination_account_id": self.destination_account_id,
            "source_account_label": self.source_account_label,
            "destination_account_label": self.destination_account_label,
            "requested_content": [str(item) for item in self.requested_content],
            "settings": self.settings.as_dict(),
            "status": str(self.status),
            "verification_status": str(self.verification_status),
            "cancellation_requested": self.cancellation_requested,
            "error_code": self.error_code,
            "total_items": self.total_items,
            "processed_items": self.processed_items,
            "successful_items": self.successful_items,
            "failed_items": self.failed_items,
            "skipped_items": self.skipped_items,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TransferJob:
        """Rebuild a job from persisted data."""

        content = value.get("requested_content") or []
        # Invariant: missing verification_status defaults to NOT_RUN for backward compatibility (Case A).
        # Explicit known values parse normally (Case B).
        # Explicit unknown/corrupted values MUST fail closed by raising InvalidPersistedStateError (Case C).
        if "verification_status" in value and value["verification_status"] is not None:
            raw_verification_status = value["verification_status"]
            try:
                verification_status = VerificationStatus(str(raw_verification_status))
            except ValueError as error:
                job_id = value.get("id", "<unknown>")
                raise InvalidPersistedStateError(
                    f"Invalid persisted verification_status '{raw_verification_status}' for job '{job_id}'"
                ) from error
        else:
            verification_status = VerificationStatus.NOT_RUN

        return cls(
            id=str(value.get("id", "")),
            user_id=value.get("user_id"),
            source_platform=Platform(str(value.get("source_platform"))),
            destination_platform=Platform(str(value.get("destination_platform"))),
            source_account_id=value.get("source_account_id"),
            destination_account_id=value.get("destination_account_id"),
            source_account_label=value.get("source_account_label"),
            destination_account_label=value.get("destination_account_label"),
            requested_content=tuple(
                ContentType(str(item)) for item in content if _is_content_type(item)
            )
            or (ContentType.LIKED_TRACKS,),
            settings=TransferSettings.from_dict(value.get("settings")),
            status=JobStatus(str(value.get("status", JobStatus.CREATED))),
            verification_status=verification_status,
            cancellation_requested=bool(value.get("cancellation_requested", False)),
            error_code=value.get("error_code"),
            total_items=int(value.get("total_items", 0)),
            processed_items=int(value.get("processed_items", 0)),
            successful_items=int(value.get("successful_items", 0)),
            failed_items=int(value.get("failed_items", 0)),
            skipped_items=int(value.get("skipped_items", 0)),
            created_at=str(value.get("created_at", utc_now())),
            started_at=value.get("started_at"),
            finished_at=value.get("finished_at"),
            updated_at=str(value.get("updated_at", utc_now())),
            metadata=dict(value.get("metadata") or {}),
        )


def _is_content_type(value: Any) -> bool:
    """Return whether a persisted content-type string is still recognized."""

    try:
        ContentType(str(value))
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------
# Item
# --------------------------------------------------------------------------


@dataclass(slots=True)
class TransferItem:
    """One resumable unit of transfer work.

    Resume is modelled at item level, never as ``last_position = 613``: after an
    interrupted run the destination may contain a different number of objects
    than the local counter suggests, so per-item status is the only trustworthy
    record (Invariant E).
    """

    id: str
    job_id: str
    entity_type: EntityType
    source_platform: Platform
    source_id: str
    destination_platform: Platform
    original_position: int = 0
    write_position: int | None = None
    destination_id: str | None = None
    container_source_id: str | None = None
    container_destination_id: str | None = None
    source_metadata: dict[str, Any] = field(default_factory=dict)
    match_method: MatchMethod = MatchMethod.NONE
    match_score: float = 0.0
    status: ItemStatus = ItemStatus.PENDING
    attempt_count: int = 0
    operation: TransferOperation = TransferOperation.NONE
    mutation_state: MutationState = MutationState.NONE
    last_error: str | None = None
    last_failure_kind: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        job_id: str,
        entity_type: EntityType,
        source_platform: Platform,
        source_id: str,
        destination_platform: Platform,
        *,
        original_position: int = 0,
        write_position: int | None = None,
        container_source_id: str | None = None,
        source_metadata: dict[str, Any] | None = None,
        operation: TransferOperation = TransferOperation.NONE,
        mutation_state: MutationState = MutationState.NONE,
    ) -> TransferItem:
        """Create a pending item with a fresh identifier."""

        return cls(
            id=new_identifier("item"),
            job_id=job_id,
            entity_type=entity_type,
            source_platform=source_platform,
            source_id=source_id,
            destination_platform=destination_platform,
            original_position=original_position,
            write_position=write_position,
            container_source_id=container_source_id,
            source_metadata=dict(source_metadata or {}),
            operation=operation,
            mutation_state=mutation_state,
        )

    def touch(self) -> None:
        """Mark the item as modified."""

        self.updated_at = utc_now()

    def mark(self, status: ItemStatus, *, error: str | None = None) -> None:
        """Advance the lifecycle status of this item.

        Every transition stamps the timestamp and clears any previous error
        unless a new one was provided.
        """

        self.status = status
        self.last_error = error
        self.updated_at = utc_now()

    def register_attempt(self) -> None:
        """Increment the attempt counter before a write is attempted."""

        self.attempt_count += 1
        self.touch()

    def is_terminal(self) -> bool:
        """Return whether the item must not be replayed automatically."""

        from ..enums import TERMINAL_ITEM_STATUSES

        return self.status in TERMINAL_ITEM_STATUSES

    def is_executable(self) -> bool:
        """Return whether this item is eligible for destination write execution.

        Central safety gate: items that failed resolution (NOT_FOUND, AMBIGUOUS,
        UNAVAILABLE) or are already satisfied or skipped must never reach
        destination mutation methods.
        """

        if self.status not in (ItemStatus.PENDING, ItemStatus.MATCHED):
            return False

        if self.operation in (
            TransferOperation.SAVE_TRACK,
            TransferOperation.SAVE_ALBUM,
            TransferOperation.FOLLOW_ARTIST,
            TransferOperation.ADD_PLAYLIST_ITEM,
        ):
            return bool(self.destination_id)

        if self.operation is TransferOperation.CREATE_PLAYLIST:
            # Creation requires playlist name metadata and must not have already landed (destination_id)
            if self.destination_id is not None:
                return False
            name = self.source_metadata.get("name") if self.source_metadata else None
            return bool(name or self.source_id)

        return False

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "id": self.id,
            "job_id": self.job_id,
            "entity_type": str(self.entity_type),
            "source_platform": str(self.source_platform),
            "source_id": self.source_id,
            "destination_platform": str(self.destination_platform),
            "destination_id": self.destination_id,
            "container_source_id": self.container_source_id,
            "container_destination_id": self.container_destination_id,
            "original_position": self.original_position,
            "write_position": self.write_position,
            "source_metadata": dict(self.source_metadata),
            "match_method": str(self.match_method),
            "match_score": self.match_score,
            "status": str(self.status),
            "operation": str(self.operation),
            "mutation_state": str(self.mutation_state),
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "last_failure_kind": self.last_failure_kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TransferItem:
        """Rebuild an item from persisted data with backward compatibility."""

        try:
            method = MatchMethod(str(value.get("match_method", MatchMethod.NONE)))
        except ValueError:
            method = MatchMethod.NONE

        entity_type = EntityType(str(value.get("entity_type")))

        raw_operation = value.get("operation")
        if raw_operation:
            try:
                operation = TransferOperation(str(raw_operation))
            except ValueError:
                operation = TransferOperation.NONE
        else:
            # Safe default / migration for legacy serialized records
            operation = {
                EntityType.TRACK: TransferOperation.SAVE_TRACK,
                EntityType.ALBUM: TransferOperation.SAVE_ALBUM,
                EntityType.ARTIST: TransferOperation.FOLLOW_ARTIST,
                EntityType.PLAYLIST: TransferOperation.CREATE_PLAYLIST,
                EntityType.PLAYLIST_ITEM: TransferOperation.ADD_PLAYLIST_ITEM,
            }.get(entity_type, TransferOperation.NONE)

        # Invariant: missing mutation_state defaults to NONE for backward compatibility (Case A).
        # Explicit known values parse normally (Case B).
        # Explicit unknown/corrupted values MUST fail closed by raising InvalidPersistedStateError (Case C),
        # ensuring that an unknown state is never converted into executable NONE.
        if "mutation_state" in value and value["mutation_state"] is not None:
            raw_mutation_state = value["mutation_state"]
            try:
                mutation_state = MutationState(str(raw_mutation_state))
            except ValueError as error:
                item_id = value.get("id", "<unknown>")
                raise InvalidPersistedStateError(
                    f"Invalid persisted mutation_state '{raw_mutation_state}' for item '{item_id}'"
                ) from error
        else:
            mutation_state = MutationState.NONE

        raw_write_position = value.get("write_position")
        write_position = int(raw_write_position) if raw_write_position is not None else None

        return cls(
            id=str(value.get("id", "")),
            job_id=str(value.get("job_id", "")),
            entity_type=entity_type,
            source_platform=Platform(str(value.get("source_platform"))),
            source_id=str(value.get("source_id", "")),
            destination_platform=Platform(str(value.get("destination_platform"))),
            destination_id=value.get("destination_id"),
            container_source_id=value.get("container_source_id"),
            container_destination_id=value.get("container_destination_id"),
            original_position=int(value.get("original_position", 0)),
            write_position=write_position,
            source_metadata=dict(value.get("source_metadata") or {}),
            match_method=method,
            match_score=float(value.get("match_score", 0.0) or 0.0),
            status=ItemStatus(str(value.get("status", ItemStatus.PENDING))),
            operation=operation,
            mutation_state=mutation_state,
            attempt_count=int(value.get("attempt_count", 0)),
            last_error=value.get("last_error"),
            last_failure_kind=value.get("last_failure_kind"),
            created_at=str(value.get("created_at", utc_now())),
            updated_at=str(value.get("updated_at", utc_now())),
        )



# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransferPlanSummary:
    """Counts shown to a user before anything is written.

    This is the data behind a message such as::

        Source: TIDAL
        Destination: Spotify

        907 tracks found
        861 exact/high-confidence matches
        21 ambiguous
        25 not found

        No destination changes have been made yet.
    """

    total_items: int = 0
    matched_items: int = 0
    ambiguous_items: int = 0
    not_found_items: int = 0
    already_exists_items: int = 0
    skipped_items: int = 0
    playlist_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "total_items": self.total_items,
            "matched_items": self.matched_items,
            "ambiguous_items": self.ambiguous_items,
            "not_found_items": self.not_found_items,
            "already_exists_items": self.already_exists_items,
            "skipped_items": self.skipped_items,
            "playlist_count": self.playlist_count,
        }


@dataclass(slots=True)
class TransferPlan:
    """A read-only description of the work a transfer would perform.

    Invariant B: producing a plan performs **no** destination mutation.  The
    planner only reads the source, searches the destination catalog, and
    inspects destination state.
    """

    job_id: str
    source_platform: Platform
    destination_platform: Platform
    created_at: str = field(default_factory=utc_now)
    items: list[TransferItem] = field(default_factory=list)
    summary: TransferPlanSummary = field(default_factory=TransferPlanSummary)
    warnings: list[str] = field(default_factory=list)
    source_incomplete: bool = False
    destination_incomplete: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "job_id": self.job_id,
            "source_platform": str(self.source_platform),
            "destination_platform": str(self.destination_platform),
            "created_at": self.created_at,
            "summary": self.summary.as_dict(),
            "warnings": list(self.warnings),
            "source_incomplete": self.source_incomplete,
            "destination_incomplete": self.destination_incomplete,
            "items": [item.as_dict() for item in self.items],
            "metadata": dict(self.metadata),
        }


# --------------------------------------------------------------------------
# Progress and reports
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransferProgress:
    """A generic progress snapshot, free of any interface concept.

    A Telegram handler converts this into ``581 / 907``; the CLI converts it
    into a progress bar.  The worker never knows which one is listening.
    """

    job_id: str
    phase: str
    total: int = 0
    processed: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    current_item: str | None = None

    @property
    def percent(self) -> float:
        """Return completion as a percentage, or ``0.0`` when the total is unknown."""

        if self.total <= 0:
            return 0.0
        return min(100.0, max(0.0, 100.0 * self.processed / self.total))

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "job_id": self.job_id,
            "phase": self.phase,
            "total": self.total,
            "processed": self.processed,
            "succeeded": self.succeeded,
            "skipped": self.skipped,
            "failed": self.failed,
            "current_item": self.current_item,
            "percent": round(self.percent, 1),
        }


@dataclass(slots=True)
class TransferReport:
    """A completed transfer's outcome, derived from stored items.

    Reports are built from :class:`TransferItem` records rather than from
    transient in-memory counters, so a report can be regenerated after a crash.
    """

    job_id: str
    operation: str = "transfer"
    started_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    total: int = 0
    transferred: int = 0
    already_existed: int = 0
    not_found: int = 0
    ambiguous: int = 0
    unavailable: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.NOT_RUN

    def finish(self) -> None:
        """Mark the report complete."""

        self.completed_at = utc_now()

    @classmethod
    def from_items(
        cls,
        job_id: str,
        items: list[TransferItem],
        operation: str = "transfer",
        verification_status: VerificationStatus = VerificationStatus.NOT_RUN,
    ) -> TransferReport:
        """Build a report by counting stored item statuses.

        This is the single place where statuses map to reported numbers, so a
        report can never disagree with the durable item state.
        """

        report = cls(
            job_id=job_id,
            operation=operation,
            total=len(items),
            verification_status=verification_status,
        )
        for item in items:
            if item.status is ItemStatus.TRANSFERRED:
                report.transferred += 1
            elif item.status is ItemStatus.ALREADY_EXISTS:
                report.already_existed += 1
            elif item.status is ItemStatus.NOT_FOUND:
                report.not_found += 1
            elif item.status is ItemStatus.AMBIGUOUS:
                report.ambiguous += 1
            elif item.status is ItemStatus.UNAVAILABLE:
                report.unavailable += 1
            elif item.status is ItemStatus.SKIPPED:
                report.skipped += 1
            elif item.status is ItemStatus.FAILED:
                report.failed += 1
                report.failures.append(
                    {
                        "entity_type": str(item.entity_type),
                        "source_id": item.source_id,
                        "reason": item.last_error or "unknown",
                    }
                )
        return report

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "format_version": 2,
            "job_id": self.job_id,
            "operation": self.operation,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "total": self.total,
            "transferred": self.transferred,
            "already_existed": self.already_existed,
            "not_found": self.not_found,
            "ambiguous": self.ambiguous,
            "unavailable": self.unavailable,
            "skipped": self.skipped,
            "failed": self.failed,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "verification_status": str(self.verification_status),
        }

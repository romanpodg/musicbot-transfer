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
    PreconditionExpectation,
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


_SENTINEL_UNSET: Any = object()


def _is_resolved_media_metadata(meta: dict[str, Any] | None) -> bool:
    """Return True if metadata describes a resolved track or video occurrence."""
    if not meta:
        return False
    kind = str(meta.get("kind") or "").lower()
    if kind in ("track", "video"):
        return True
    if kind == "unresolved":
        return False
    return bool("isrc" in meta or "artists" in meta or "duration_ms" in meta)


def _infer_legacy_playlist_item_type(meta: dict[str, Any] | None) -> EntityType | None:
    """Infer media type for legacy records where playlist_item_type is absent."""
    if not meta:
        return EntityType.TRACK
    kind = str(meta.get("kind") or "").lower()
    if kind == "video":
        return EntityType.VIDEO
    if kind == "unresolved":
        return None
    if kind in ("", "track"):
        return EntityType.TRACK
    return None


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

    @classmethod
    def from_plan_dict(cls, value: dict[str, Any] | None) -> TransferSettings:
        """Strict settings parser for TransferPlan integrity verification.

        Fails closed on explicitly invalid values or unrecognized enum members.
        """
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise InvalidPersistedStateError(
                "invalid_persisted_state", "Plan settings must be a dict"
            )

        raw_ordering = value.get("ordering")
        if raw_ordering is not None:
            try:
                ordering = OrderingMode(str(raw_ordering))
            except ValueError as err:
                raise InvalidPersistedStateError(
                    "invalid_persisted_state",
                    f"Invalid ordering mode in plan settings: '{raw_ordering}'",
                ) from err
        else:
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


def canonicalize_metadata(value: Any) -> Any:
    """Deterministically normalize source metadata for canonical plan hashing and comparison."""
    if isinstance(value, dict):
        return {str(k): canonicalize_metadata(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonicalize_metadata(v) for v in value]
    return value


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
    active_plan_id: str | None = None
    active_plan_revision: int | None = None
    active_plan_hash: str | None = None
    confirmed_plan_id: str | None = None
    confirmed_plan_revision: int | None = None
    confirmed_plan_hash: str | None = None
    confirmed_at: str | None = None
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
            "active_plan_id": self.active_plan_id,
            "active_plan_revision": self.active_plan_revision,
            "active_plan_hash": self.active_plan_hash,
            "confirmed_plan_id": self.confirmed_plan_id,
            "confirmed_plan_revision": self.confirmed_plan_revision,
            "confirmed_plan_hash": self.confirmed_plan_hash,
            "confirmed_at": self.confirmed_at,
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
            active_plan_id=value.get("active_plan_id"),
            active_plan_revision=(
                int(value["active_plan_revision"])
                if value.get("active_plan_revision") is not None
                else None
            ),
            active_plan_hash=value.get("active_plan_hash"),
            confirmed_plan_id=value.get("confirmed_plan_id"),
            confirmed_plan_revision=(
                int(value["confirmed_plan_revision"])
                if value.get("confirmed_plan_revision") is not None
                else None
            ),
            confirmed_plan_hash=value.get("confirmed_plan_hash"),
            confirmed_at=value.get("confirmed_at"),
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
    playlist_item_type: EntityType | None = _SENTINEL_UNSET  # type: ignore[assignment]
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.playlist_item_type is not _SENTINEL_UNSET and self.playlist_item_type is not None:
            if self.playlist_item_type not in (EntityType.TRACK, EntityType.VIDEO):
                raise InvalidPersistedStateError(
                    f"Unsupported playlist_item_type '{self.playlist_item_type}'"
                )
            if self.entity_type is not EntityType.PLAYLIST_ITEM:
                raise InvalidPersistedStateError(
                    f"playlist_item_type '{self.playlist_item_type}' not allowed for entity_type '{self.entity_type}'"
                )
        elif self.playlist_item_type is None:
            if self.entity_type is EntityType.PLAYLIST_ITEM and _is_resolved_media_metadata(
                self.source_metadata
            ):
                kind_label = (self.source_metadata or {}).get("kind", "media")
                raise InvalidPersistedStateError(
                    f"Contradictory persisted playlist item: playlist_item_type is null but metadata describes a resolved {kind_label}"
                )
        elif self.playlist_item_type is _SENTINEL_UNSET:
            if self.entity_type is EntityType.PLAYLIST_ITEM:
                self.playlist_item_type = _infer_legacy_playlist_item_type(self.source_metadata)
            else:
                self.playlist_item_type = None

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
        playlist_item_type: Any = _SENTINEL_UNSET,
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
            playlist_item_type=playlist_item_type,
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
            TransferOperation.SAVE_VIDEO,
            TransferOperation.SAVE_MIX,
            TransferOperation.ADD_PLAYLIST_ITEM,
        ):
            return bool(self.destination_id)

        if self.operation is TransferOperation.CREATE_PLAYLIST:
            # Creation requires playlist name metadata and must not have already landed (destination_id)
            if self.destination_id is not None:
                return False
            name = self.source_metadata.get("name") if self.source_metadata else None
            return bool(name or self.source_id)

        if self.operation is TransferOperation.CREATE_FOLDER:
            # Creation requires non-empty folder name metadata and must not have already landed (destination_id)
            if self.destination_id is not None:
                return False
            name = (
                self.source_metadata.get("name") or self.source_metadata.get("title")
                if self.source_metadata
                else None
            )
            return bool(name and str(name).strip())

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
            "playlist_item_type": (
                self.playlist_item_type.value if self.playlist_item_type is not None else None
            ),
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
                EntityType.VIDEO: TransferOperation.SAVE_VIDEO,
                EntityType.MIX: TransferOperation.SAVE_MIX,
                EntityType.PLAYLIST: TransferOperation.CREATE_PLAYLIST,
                EntityType.PLAYLIST_ITEM: TransferOperation.ADD_PLAYLIST_ITEM,
                EntityType.FOLDER: TransferOperation.CREATE_FOLDER,
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

        if "playlist_item_type" in value:
            raw_playlist_item_type = value["playlist_item_type"]
            if raw_playlist_item_type is not None:
                try:
                    parsed_type = EntityType(str(raw_playlist_item_type))
                except ValueError as err:
                    raise InvalidPersistedStateError(
                        f"Invalid persisted playlist_item_type '{raw_playlist_item_type}'"
                    ) from err
                if parsed_type not in (EntityType.TRACK, EntityType.VIDEO):
                    raise InvalidPersistedStateError(
                        f"Unsupported playlist_item_type '{raw_playlist_item_type}'"
                    )
                if entity_type is not EntityType.PLAYLIST_ITEM:
                    raise InvalidPersistedStateError(
                        f"playlist_item_type '{parsed_type}' not allowed for entity_type '{entity_type}'"
                    )
                playlist_item_type = parsed_type
            else:
                # Explicit null: do NOT infer TRACK/VIDEO
                if entity_type is EntityType.PLAYLIST_ITEM and _is_resolved_media_metadata(
                    value.get("source_metadata")
                ):
                    kind_label = (value.get("source_metadata") or {}).get("kind", "media")
                    raise InvalidPersistedStateError(
                        f"Contradictory persisted playlist item: playlist_item_type is null but metadata describes a resolved {kind_label}"
                    )
                playlist_item_type = None
        else:
            # Field ABSENT: legacy inference allowed
            if entity_type is EntityType.PLAYLIST_ITEM:
                playlist_item_type = _infer_legacy_playlist_item_type(value.get("source_metadata"))
            else:
                playlist_item_type = None

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
            playlist_item_type=playlist_item_type,
            last_error=value.get("last_error"),
            last_failure_kind=value.get("last_failure_kind"),
            created_at=str(value.get("created_at", utc_now())),
            updated_at=str(value.get("updated_at", utc_now())),
        )

    def intent_payload(self) -> dict[str, Any]:
        """Return the canonical execution intent dictionary matching TransferPlanItem."""
        return {
            "container_destination_id": self.container_destination_id,
            "container_source_id": self.container_source_id,
            "destination_id": self.destination_id,
            "entity_type": self.entity_type.value,
            "match_method": self.match_method.value,
            "match_score": round(self.match_score, 4),
            "operation": self.operation.value,
            "original_position": self.original_position,
            "planned_status": self.status.value,
            "playlist_item_type": (
                self.playlist_item_type.value if self.playlist_item_type is not None else None
            ),
            "source_id": self.source_id,
            "source_metadata": canonicalize_metadata(self.source_metadata),
            "write_position": self.write_position,
        }



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


@dataclass(frozen=True, slots=True)
class TransferPlanItem:
    """An immutable approved plan item snapshot.

    Invariant G: Runtime execution item updates do not mutate the approved plan.
    """

    entity_type: EntityType
    source_id: str
    destination_id: str | None
    operation: TransferOperation
    planned_status: ItemStatus
    match_method: MatchMethod
    match_score: float
    container_source_id: str | None = None
    container_destination_id: str | None = None
    original_position: int | None = None
    write_position: int | None = None
    playlist_item_type: EntityType | None = _SENTINEL_UNSET  # type: ignore[assignment]
    source_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.playlist_item_type is not _SENTINEL_UNSET and self.playlist_item_type is not None:
            if self.playlist_item_type not in (EntityType.TRACK, EntityType.VIDEO):
                raise InvalidPersistedStateError(
                    f"Unsupported playlist_item_type '{self.playlist_item_type}'"
                )
            if self.entity_type is not EntityType.PLAYLIST_ITEM:
                raise InvalidPersistedStateError(
                    f"playlist_item_type '{self.playlist_item_type}' not allowed for entity_type '{self.entity_type}'"
                )
        elif self.playlist_item_type is None:
            if self.entity_type is EntityType.PLAYLIST_ITEM and _is_resolved_media_metadata(
                self.source_metadata
            ):
                kind_label = (self.source_metadata or {}).get("kind", "media")
                raise InvalidPersistedStateError(
                    f"Contradictory persisted plan item: playlist_item_type is null but metadata describes a resolved {kind_label}"
                )
        elif self.playlist_item_type is _SENTINEL_UNSET:
            if self.entity_type is EntityType.PLAYLIST_ITEM:
                object.__setattr__(
                    self,
                    "playlist_item_type",
                    _infer_legacy_playlist_item_type(self.source_metadata),
                )
            else:
                object.__setattr__(self, "playlist_item_type", None)

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""
        return {
            "entity_type": self.entity_type.value,
            "source_id": self.source_id,
            "destination_id": self.destination_id,
            "operation": self.operation.value,
            "planned_status": self.planned_status.value,
            "match_method": self.match_method.value,
            "match_score": self.match_score,
            "container_source_id": self.container_source_id,
            "container_destination_id": self.container_destination_id,
            "original_position": self.original_position,
            "write_position": self.write_position,
            "playlist_item_type": (
                self.playlist_item_type.value if self.playlist_item_type is not None else None
            ),
            "source_metadata": dict(self.source_metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TransferPlanItem:
        """Rebuild a plan item snapshot from persisted data."""
        entity_type = EntityType(str(value.get("entity_type")))
        if "playlist_item_type" in value:
            raw_playlist_item_type = value["playlist_item_type"]
            if raw_playlist_item_type is not None:
                try:
                    parsed_type = EntityType(str(raw_playlist_item_type))
                except ValueError as err:
                    raise InvalidPersistedStateError(
                        f"Invalid persisted playlist_item_type '{raw_playlist_item_type}'"
                    ) from err
                if parsed_type not in (EntityType.TRACK, EntityType.VIDEO):
                    raise InvalidPersistedStateError(
                        f"Unsupported playlist_item_type '{raw_playlist_item_type}'"
                    )
                if entity_type is not EntityType.PLAYLIST_ITEM:
                    raise InvalidPersistedStateError(
                        f"playlist_item_type '{parsed_type}' not allowed for entity_type '{entity_type}'"
                    )
                playlist_item_type = parsed_type
            else:
                # Explicit null: do NOT infer TRACK/VIDEO
                if entity_type is EntityType.PLAYLIST_ITEM and _is_resolved_media_metadata(
                    value.get("source_metadata")
                ):
                    kind_label = (value.get("source_metadata") or {}).get("kind", "media")
                    raise InvalidPersistedStateError(
                        f"Contradictory persisted plan item: playlist_item_type is null but metadata describes a resolved {kind_label}"
                    )
                playlist_item_type = None
        else:
            # Field ABSENT: legacy inference allowed
            if entity_type is EntityType.PLAYLIST_ITEM:
                playlist_item_type = _infer_legacy_playlist_item_type(value.get("source_metadata"))
            else:
                playlist_item_type = None

        return cls(
            entity_type=entity_type,
            source_id=str(value.get("source_id", "")),
            destination_id=value.get("destination_id"),
            operation=TransferOperation(str(value.get("operation", TransferOperation.NONE.value))),
            planned_status=ItemStatus(str(value.get("planned_status", ItemStatus.PENDING.value))),
            match_method=MatchMethod(str(value.get("match_method", MatchMethod.NONE.value))),
            match_score=float(value.get("match_score", 0.0)),
            container_source_id=value.get("container_source_id"),
            container_destination_id=value.get("container_destination_id"),
            original_position=(
                int(value["original_position"])
                if value.get("original_position") is not None
                else None
            ),
            write_position=(
                int(value["write_position"])
                if value.get("write_position") is not None
                else None
            ),
            playlist_item_type=playlist_item_type,
            source_metadata=dict(value.get("source_metadata") or {}),
        )

    def intent_payload(self) -> dict[str, Any]:
        """Return the authoritative dictionary representing approved execution intent."""
        return {
            "container_destination_id": self.container_destination_id,
            "container_source_id": self.container_source_id,
            "destination_id": self.destination_id,
            "entity_type": self.entity_type.value,
            "match_method": self.match_method.value,
            "match_score": round(self.match_score, 4),
            "operation": self.operation.value,
            "original_position": self.original_position,
            "planned_status": self.planned_status.value,
            "playlist_item_type": (
                self.playlist_item_type.value if self.playlist_item_type is not None else None
            ),
            "source_id": self.source_id,
            "source_metadata": canonicalize_metadata(self.source_metadata),
            "write_position": self.write_position,
        }

    def canonical_dict(self) -> dict[str, Any]:
        """Return the canonical dictionary used for deterministic integrity hashing."""
        return self.intent_payload()


VALID_PRECONDITION_SECTIONS: frozenset[str] = frozenset(
    {"tracks", "albums", "artists", "videos", "mixes", "playlists"}
)


@dataclass(frozen=True, slots=True)
class PlanPrecondition:
    """A deterministic destination precondition derived during planning."""

    entity_type: EntityType
    destination_id: str
    expected: PreconditionExpectation | str  # "present" | "absent"
    section: str   # "tracks" | "albums" | "artists" | "playlists"

    def __post_init__(self) -> None:
        if not isinstance(self.entity_type, EntityType):
            try:
                object.__setattr__(self, "entity_type", EntityType(str(self.entity_type)))
            except ValueError as err:
                raise InvalidPersistedStateError(
                    "invalid_persisted_state",
                    f"Invalid precondition entity_type: '{self.entity_type}'",
                ) from err

        if not isinstance(self.expected, PreconditionExpectation):
            try:
                object.__setattr__(
                    self, "expected", PreconditionExpectation(str(self.expected))
                )
            except ValueError as err:
                raise InvalidPersistedStateError(
                    "invalid_persisted_state",
                    f"Invalid precondition expected: '{self.expected}'",
                ) from err

        if self.section not in VALID_PRECONDITION_SECTIONS:
            raise InvalidPersistedStateError(
                "invalid_persisted_state",
                f"Invalid precondition section: '{self.section}'",
            )

        if not self.destination_id:
            raise InvalidPersistedStateError(
                "invalid_persisted_state",
                "Precondition destination_id cannot be empty",
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type.value,
            "destination_id": self.destination_id,
            "expected": str(self.expected),
            "section": self.section,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PlanPrecondition:
        if not isinstance(value, dict):
            raise InvalidPersistedStateError(
                "invalid_persisted_state", "Invalid precondition record: must be a dict"
            )
        raw_entity_type = value.get("entity_type")
        try:
            entity_type = EntityType(str(raw_entity_type))
        except ValueError as err:
            raise InvalidPersistedStateError(
                "invalid_persisted_state",
                f"Invalid precondition entity_type: '{raw_entity_type}'",
            ) from err

        raw_expected = value.get("expected")
        try:
            expected = PreconditionExpectation(str(raw_expected))
        except ValueError as err:
            raise InvalidPersistedStateError(
                "invalid_persisted_state",
                f"Invalid precondition expected: '{raw_expected}'",
            ) from err

        raw_section = str(value.get("section", ""))
        if raw_section not in VALID_PRECONDITION_SECTIONS:
            raise InvalidPersistedStateError(
                "invalid_persisted_state",
                f"Invalid precondition section: '{raw_section}'",
            )

        destination_id = str(value.get("destination_id", ""))
        if not destination_id:
            raise InvalidPersistedStateError(
                "invalid_persisted_state",
                "Precondition destination_id cannot be empty",
            )

        return cls(
            entity_type=entity_type,
            destination_id=destination_id,
            expected=expected,
            section=raw_section,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "destination_id": self.destination_id,
            "entity_type": self.entity_type.value,
            "expected": str(self.expected),
            "section": self.section,
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
    plan_id: str = ""
    revision: int = 1
    plan_hash: str = ""
    schema_version: int = 1
    created_at: str = field(default_factory=utc_now)
    items: tuple[TransferPlanItem, ...] = ()
    summary: TransferPlanSummary = field(default_factory=TransferPlanSummary)
    preconditions: tuple[PlanPrecondition, ...] = ()
    warnings: list[str] = field(default_factory=list)
    source_incomplete: bool = False
    destination_incomplete: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def canonical_payload(self) -> dict[str, Any]:
        """Produce the canonical sorted dictionary for deterministic SHA-256 hashing.

        Excludes: plan_hash, created_at, runtime execution state.
        Includes: schema_version, job_id, platforms, incompleteness flags,
        full material settings/context, approved-order items, sorted preconditions.
        """
        raw_settings = self.metadata.get("settings")
        if raw_settings is not None:
            if not isinstance(raw_settings, dict):
                raise InvalidPersistedStateError(
                    "invalid_persisted_state", "Plan settings metadata must be a dictionary"
                )
            settings_dict = TransferSettings.from_plan_dict(raw_settings).as_dict()
        else:
            raw_ordering = self.metadata.get("ordering", OrderingMode.SOURCE_ORDER.value)
            try:
                ordering = OrderingMode(str(raw_ordering))
            except ValueError as err:
                raise InvalidPersistedStateError(
                    "invalid_persisted_state",
                    f"Invalid ordering mode in plan metadata: '{raw_ordering}'",
                ) from err
            settings_dict = {
                "ordering": str(ordering),
                "preserve_visible_order": bool(self.metadata.get("preserve_visible_order", False)),
                "allow_explicit_to_clean_fallback": bool(self.metadata.get("allow_explicit_to_clean_fallback", False)),
                "allow_duplicates_in_playlists": bool(self.metadata.get("allow_duplicates_in_playlists", True)),
                "skip_already_existing": bool(self.metadata.get("skip_already_existing", True)),
                "max_item_attempts": int(self.metadata.get("max_item_attempts", 3)),
                "dry_run": bool(self.metadata.get("dry_run", False)),
            }

        raw_content = self.metadata.get("requested_content")
        if isinstance(raw_content, (list, tuple)):
            requested_content = [str(c) for c in raw_content]
        else:
            requested_content = ["liked_tracks"]

        source_account_id = self.metadata.get("source_account_id")
        destination_account_id = self.metadata.get("destination_account_id")

        items_payload = [item.canonical_dict() for item in self.items]

        sorted_preconditions = sorted(
            [p.canonical_dict() for p in self.preconditions],
            key=lambda d: (
                d["entity_type"],
                d["destination_id"],
                d["expected"],
                d["section"],
            ),
        )

        return {
            "destination_account_id": str(destination_account_id) if destination_account_id is not None else None,
            "destination_incomplete": bool(self.destination_incomplete),
            "destination_platform": self.destination_platform.value,
            "items": items_payload,
            "job_id": self.job_id,
            "preconditions": sorted_preconditions,
            "requested_content": requested_content,
            "schema_version": self.schema_version,
            "settings": settings_dict,
            "source_account_id": str(source_account_id) if source_account_id is not None else None,
            "source_incomplete": bool(self.source_incomplete),
            "source_platform": self.source_platform.value,
        }

    def compute_hash(self) -> str:
        """Compute deterministic SHA-256 hex digest of canonical plan payload."""
        import hashlib
        import json

        payload = self.canonical_payload()
        canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        """Verify that persisted plan_hash matches recomputed hash from plan content."""
        return bool(self.plan_hash) and self.compute_hash() == self.plan_hash

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "plan_hash": self.plan_hash,
            "job_id": self.job_id,
            "source_platform": str(self.source_platform),
            "destination_platform": str(self.destination_platform),
            "created_at": self.created_at,
            "summary": self.summary.as_dict(),
            "warnings": list(self.warnings),
            "source_incomplete": self.source_incomplete,
            "destination_incomplete": self.destination_incomplete,
            "items": [item.as_dict() for item in self.items],
            "preconditions": [p.as_dict() for p in self.preconditions],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TransferPlan:
        """Rebuild a plan from persisted data."""
        raw_items = value.get("items") or []
        items: list[TransferPlanItem] = []
        for item in raw_items:
            if isinstance(item, TransferPlanItem):
                items.append(item)
            elif isinstance(item, dict):
                # Check if it has TransferPlanItem fields vs TransferItem fields
                if "planned_status" in item:
                    items.append(TransferPlanItem.from_dict(item))
                else:
                    # Legacy TransferItem representation
                    legacy_entity = EntityType(str(item.get("entity_type", EntityType.TRACK.value)))
                    if "playlist_item_type" in item:
                        raw_pit = item["playlist_item_type"]
                        if raw_pit is not None:
                            try:
                                parsed_pit = EntityType(str(raw_pit))
                            except ValueError as err:
                                raise InvalidPersistedStateError(
                                    f"Invalid persisted playlist_item_type '{raw_pit}'"
                                ) from err
                            if parsed_pit not in (EntityType.TRACK, EntityType.VIDEO):
                                raise InvalidPersistedStateError(
                                    f"Unsupported playlist_item_type '{raw_pit}'"
                                )
                            if legacy_entity is not EntityType.PLAYLIST_ITEM:
                                raise InvalidPersistedStateError(
                                    f"playlist_item_type '{parsed_pit}' not allowed for entity_type '{legacy_entity}'"
                                )
                            pit = parsed_pit
                        else:
                            # Explicit null: do NOT infer TRACK/VIDEO
                            if legacy_entity is EntityType.PLAYLIST_ITEM and _is_resolved_media_metadata(
                                item.get("source_metadata")
                            ):
                                kind_label = (item.get("source_metadata") or {}).get("kind", "media")
                                raise InvalidPersistedStateError(
                                    f"Contradictory persisted plan item: playlist_item_type is null but metadata describes a resolved {kind_label}"
                                )
                            pit = None
                    elif legacy_entity is EntityType.PLAYLIST_ITEM:
                        # Field ABSENT: legacy inference allowed
                        pit = _infer_legacy_playlist_item_type(item.get("source_metadata"))
                    else:
                        pit = None
                    items.append(
                        TransferPlanItem(
                            entity_type=legacy_entity,
                            source_id=str(item.get("source_id", "")),
                            destination_id=item.get("destination_id"),
                            operation=TransferOperation(str(item.get("operation", TransferOperation.NONE.value))),
                            planned_status=ItemStatus(str(item.get("status", ItemStatus.PENDING.value))),
                            match_method=MatchMethod(str(item.get("match_method", MatchMethod.NONE.value))),
                            match_score=float(item.get("match_score", 0.0)),
                            container_source_id=item.get("container_source_id"),
                            container_destination_id=item.get("container_destination_id"),
                            original_position=item.get("original_position"),
                            write_position=item.get("write_position"),
                            playlist_item_type=pit,
                            source_metadata=dict(item.get("source_metadata") or {}),
                        )
                    )

        raw_preconditions = value.get("preconditions") or []
        preconditions_list: list[PlanPrecondition] = []
        for p in raw_preconditions:
            if isinstance(p, PlanPrecondition):
                preconditions_list.append(p)
            elif isinstance(p, dict):
                preconditions_list.append(PlanPrecondition.from_dict(p))
            else:
                raise InvalidPersistedStateError(
                    "invalid_persisted_state", f"Invalid precondition entry: {p}"
                )
        preconditions = tuple(preconditions_list)

        summary_data = value.get("summary")
        summary = (
            TransferPlanSummary(**summary_data)
            if isinstance(summary_data, dict)
            else TransferPlanSummary()
        )

        return cls(
            schema_version=int(value.get("schema_version", 1)),
            plan_id=str(value.get("plan_id", "")),
            revision=int(value.get("revision", 1)),
            plan_hash=str(value.get("plan_hash", "")),
            job_id=str(value.get("job_id", "")),
            source_platform=Platform(str(value.get("source_platform"))),
            destination_platform=Platform(str(value.get("destination_platform"))),
            created_at=str(value.get("created_at", utc_now())),
            items=tuple(items),
            summary=summary,
            preconditions=preconditions,
            warnings=[str(w) for w in (value.get("warnings") or [])],
            source_incomplete=bool(value.get("source_incomplete", False)),
            destination_incomplete=bool(value.get("destination_incomplete", False)),
            metadata=dict(value.get("metadata") or {}),
        )

    @classmethod
    def create(
        cls,
        job_id: str,
        *,
        source_platform: Platform = Platform.TIDAL,
        destination_platform: Platform = Platform.TIDAL,
        revision: int = 1,
        items: tuple[TransferPlanItem, ...] = (),
        summary: TransferPlanSummary | None = None,
        preconditions: tuple[PlanPrecondition, ...] = (),
        warnings: list[str] | None = None,
        source_incomplete: bool = False,
        destination_incomplete: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> TransferPlan:
        """Create a new TransferPlan with a fresh plan_id and computed plan_hash."""
        plan = cls(
            schema_version=1,
            plan_id=new_identifier("plan"),
            revision=revision,
            plan_hash="",
            job_id=job_id,
            source_platform=source_platform,
            destination_platform=destination_platform,
            created_at=utc_now(),
            items=items,
            summary=summary or TransferPlanSummary(total_items=len(items)),
            preconditions=preconditions,
            warnings=list(warnings or []),
            source_incomplete=source_incomplete,
            destination_incomplete=destination_incomplete,
            metadata=dict(metadata or {}),
        )
        plan.plan_hash = plan.compute_hash()
        return plan


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

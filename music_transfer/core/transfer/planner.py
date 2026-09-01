"""The read-only transfer planner.

Invariant B: **a plan performs no destination mutation.**  The destination
adapter is always accessed through
:class:`~music_transfer.core.ports.platform.ReadOnlyAdapter`, so an accidental
write raises instead of silently changing a user's library.

The planner turns an exported source snapshot into ordered
:class:`TransferItem` records plus a
:class:`TransferPlanSummary` suitable for a confirmation screen such as::

    Source: TIDAL
    Destination: Spotify

    907 tracks found
    861 exact/high-confidence matches
    21 ambiguous
    25 not found

    No destination changes have been made yet.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..domain import (
    IdentifierResolution,
    LibrarySnapshot,
    PlanPrecondition,
    Playlist,
    Track,
    TransferItem,
    TransferJob,
    TransferPlan,
    TransferPlanItem,
    TransferPlanSummary,
)
from ..enums import (
    ContentType,
    DestinationPresence,
    EntityType,
    IdentifierResolutionPolicy,
    ItemStatus,
    MatchMethod,
    MatchOutcome,
    Platform,
    PreconditionExpectation,
    TransferOperation,
)
from ..errors import (
    DestinationPresenceUnknownError,
    InvalidDestinationSectionError,
    TransferConfigurationError,
    UnsupportedCapabilityError,
    UnsupportedTransferContentError,
)
from ..matching import AlbumMatcher, ArtistMatcher, TrackMatcher
from ..ports import DestinationState, MusicPlatformReadPort, PlatformCapabilities
from .ordering import apply_logical_order

_LOGGER = logging.getLogger("music_transfer.planner")


#: Match methods recorded when no catalog search is required.
_DIRECT_METHOD = MatchMethod.DIRECT_ID
_NONE_METHOD = MatchMethod.NONE


@dataclass(frozen=True, slots=True)
class TransferContentSpec:
    """Authoritative transfer specification for a single content type."""

    content_type: ContentType
    snapshot_sections: tuple[str, ...]
    entity_type: EntityType
    operation: TransferOperation
    resolution_policy: IdentifierResolutionPolicy
    source_read_capabilities: tuple[str, ...]
    destination_write_capabilities: tuple[str, ...]
    search_capability: str | None = None


#: Engine support registry: only content types with complete transfer paths.
ENGINE_TRANSFER_SPECS: dict[ContentType, TransferContentSpec] = {
    ContentType.LIKED_TRACKS: TransferContentSpec(
        content_type=ContentType.LIKED_TRACKS,
        snapshot_sections=("tracks",),
        entity_type=EntityType.TRACK,
        operation=TransferOperation.SAVE_TRACK,
        resolution_policy=IdentifierResolutionPolicy.REUSE_OR_SEARCH,
        search_capability="search_tracks",
        source_read_capabilities=("read_liked_tracks",),
        destination_write_capabilities=("write_liked_tracks",),
    ),
    ContentType.SAVED_ALBUMS: TransferContentSpec(
        content_type=ContentType.SAVED_ALBUMS,
        snapshot_sections=("albums",),
        entity_type=EntityType.ALBUM,
        operation=TransferOperation.SAVE_ALBUM,
        resolution_policy=IdentifierResolutionPolicy.REUSE_OR_SEARCH,
        search_capability="search_albums",
        source_read_capabilities=("read_saved_albums",),
        destination_write_capabilities=("write_saved_albums",),
    ),
    ContentType.FOLLOWED_ARTISTS: TransferContentSpec(
        content_type=ContentType.FOLLOWED_ARTISTS,
        snapshot_sections=("artists",),
        entity_type=EntityType.ARTIST,
        operation=TransferOperation.FOLLOW_ARTIST,
        resolution_policy=IdentifierResolutionPolicy.REUSE_OR_SEARCH,
        search_capability="search_artists",
        source_read_capabilities=("read_followed_artists",),
        destination_write_capabilities=("write_followed_artists",),
    ),
    ContentType.PLAYLISTS: TransferContentSpec(
        content_type=ContentType.PLAYLISTS,
        snapshot_sections=("playlists",),
        entity_type=EntityType.PLAYLIST,
        operation=TransferOperation.CREATE_PLAYLIST,
        resolution_policy=IdentifierResolutionPolicy.CONTAINER_CREATE,
        search_capability=None,
        source_read_capabilities=("read_playlists",),
        destination_write_capabilities=("create_playlists", "write_playlist_items"),
    ),
}

#: Backward-compatible alias
CONTENT_SPECS = ENGINE_TRANSFER_SPECS


def require_transfer_content_spec(content_type: ContentType) -> TransferContentSpec:
    """Resolve the authoritative transfer spec for a content type.

    Raises:
        UnsupportedTransferContentError: If the content type is not implemented
            by the transfer engine.
    """
    spec = ENGINE_TRANSFER_SPECS.get(content_type)
    if spec is None:
        raise UnsupportedTransferContentError(
            "unsupported_transfer_content",
            content_type=content_type,
            reason="engine_not_implemented",
        )
    return spec


def validate_transfer_content_support(
    requested_content: tuple[ContentType, ...] | list[ContentType],
    source_capabilities: PlatformCapabilities,
    destination_capabilities: PlatformCapabilities,
) -> None:
    """Validate that every requested content type is supported end-to-end.

    Rule:
        source can read ∩ destination can write ∩ engine implements the complete transfer path

    Raises:
        UnsupportedTransferContentError: If any requested content type fails
            engine support, source read capability, or destination write capability.
    """
    for content_type in requested_content:
        spec = require_transfer_content_spec(content_type)
        for read_cap in spec.source_read_capabilities:
            if not source_capabilities.supports(read_cap):
                raise UnsupportedTransferContentError(
                    "unsupported_transfer_content",
                    content_type=content_type,
                    reason="source_read_unsupported",
                    capability=read_cap,
                )
        for write_cap in spec.destination_write_capabilities:
            if not destination_capabilities.supports(write_cap):
                raise UnsupportedTransferContentError(
                    "unsupported_transfer_content",
                    content_type=content_type,
                    reason="destination_write_unsupported",
                    capability=write_cap,
                )


def content_sections(
    content: tuple[ContentType, ...] | list[ContentType] | Iterable[ContentType],
) -> tuple[str, ...]:
    """Return the snapshot sections required by a set of content types."""

    sections: set[str] = set()
    for item in content:
        spec = require_transfer_content_spec(item)
        sections.update(spec.snapshot_sections)
    return tuple(sorted(sections))


@dataclass(frozen=True, slots=True)
class PlannerResult:

    """The outcome of planning one job."""

    plan: TransferPlan
    items: list[TransferItem]

    @property
    def summary(self) -> TransferPlanSummary:
        """Return the user-facing counts."""

        return self.plan.summary


class TransferPlanner:
    """Build a transfer plan from an exported snapshot without writing anything."""

    def __init__(
        self,
        matcher: TrackMatcher | None = None,
        logger: logging.Logger | None = None,
        *,
        album_matcher: AlbumMatcher | None = None,
        artist_matcher: ArtistMatcher | None = None,
    ) -> None:
        self._matcher = matcher or TrackMatcher()
        self._album_matcher = album_matcher or AlbumMatcher(self._matcher.policy)
        self._artist_matcher = artist_matcher or ArtistMatcher(self._matcher.policy)
        self._logger = logger or _LOGGER

    @property
    def matcher(self) -> TrackMatcher:
        """Return the matcher used for cross-platform track catalog resolution."""

        return self._matcher

    @property
    def album_matcher(self) -> AlbumMatcher:
        """Return the matcher used for cross-platform album catalog resolution."""

        return self._album_matcher

    @property
    def artist_matcher(self) -> ArtistMatcher:
        """Return the matcher used for cross-platform artist catalog resolution."""

        return self._artist_matcher

    def build(
        self,
        job: TransferJob,
        source: LibrarySnapshot,
        destination: MusicPlatformReadPort,
        *,
        destination_state: DestinationState | None = None,
    ) -> PlannerResult:
        """Produce a plan for one job.

        Args:
            job: The job being planned.
            source: The already-exported source snapshot.
            destination: The destination read port.
            destination_state: A previously captured destination state.  When
                omitted, the planner reads it (still read-only).

        Raises:
            UnsupportedTransferContentError: If a requested content type is not
                engine-supported or missing required capabilities.
            UnsupportedCapabilityError: If the destination cannot perform a
                requested write, or if the source snapshot is missing a
                requested section.
        """

        capabilities = destination.capabilities
        state = destination_state
        if state is None:
            state = self._read_destination_state(destination, job)
        items: list[TransferItem] = []
        warnings: list[str] = []
        for content_type in job.requested_content:
            require_transfer_content_spec(content_type)
            if content_type is ContentType.PLAYLISTS:
                items.extend(
                    self._plan_playlists(job, source, destination, capabilities, state, warnings)
                )
                continue
            items.extend(
                self._plan_section(job, source, destination, capabilities, state, content_type, warnings)
            )
        self._validate_plan(items)
        summary = self._summarize(items, source, state)

        # Build immutable TransferPlanItem snapshots (Invariant G)
        plan_items = tuple(
            TransferPlanItem(
                entity_type=it.entity_type,
                source_id=it.source_id,
                destination_id=it.destination_id,
                operation=it.operation,
                planned_status=it.status,
                match_method=it.match_method,
                match_score=it.match_score,
                container_source_id=it.container_source_id,
                container_destination_id=it.container_destination_id,
                original_position=it.original_position,
                write_position=it.write_position,
                source_metadata=dict(it.source_metadata),
            )
            for it in items
        )

        # Build trustworthy destination preconditions based on observed presence (Invariants I, J, Phase 1.4C)
        preconditions: list[PlanPrecondition] = []
        if state is not None:
            for it in items:
                if not it.destination_id:
                    continue
                try:
                    presence = state.presence(it.entity_type, it.destination_id)
                except InvalidDestinationSectionError:
                    continue

                if it.entity_type is EntityType.TRACK:
                    section = "tracks"
                elif it.entity_type is EntityType.ALBUM:
                    section = "albums"
                elif it.entity_type is EntityType.ARTIST:
                    section = "artists"
                elif it.entity_type is EntityType.PLAYLIST:
                    section = "playlists"
                else:
                    continue

                if presence is DestinationPresence.PRESENT and it.status is ItemStatus.ALREADY_EXISTS:
                    preconditions.append(
                        PlanPrecondition(
                            entity_type=it.entity_type,
                            destination_id=it.destination_id,
                            expected=PreconditionExpectation.PRESENT,
                            section=section,
                        )
                    )
                elif (
                    presence is DestinationPresence.ABSENT
                    and it.status is ItemStatus.MATCHED
                    and it.operation != TransferOperation.NONE
                ):
                    preconditions.append(
                        PlanPrecondition(
                            entity_type=it.entity_type,
                            destination_id=it.destination_id,
                            expected=PreconditionExpectation.ABSENT,
                            section=section,
                        )
                    )


        plan = TransferPlan(
            job_id=job.id,
            source_platform=job.source_platform,
            destination_platform=job.destination_platform,
            items=plan_items,
            summary=summary,
            preconditions=tuple(preconditions),
            warnings=warnings,
            source_incomplete=source.is_partial,
            metadata={
                "destination_state": state.as_dict() if state is not None else None,
                "settings": job.settings.as_dict(),
                "requested_content": [c.value for c in job.requested_content],
                "source_account_id": job.source_account_id,
                "destination_account_id": job.destination_account_id,
                "source_platform": job.source_platform.value,
                "destination_platform": job.destination_platform.value,
            },
        )
        self._logger.info(
            "event=plan_built job_id=%s items=%d matched=%d ambiguous=%d not_found=%d preconditions=%d",
            job.id,
            summary.total_items,
            summary.matched_items,
            summary.ambiguous_items,
            summary.not_found_items,
            len(preconditions),
        )
        return PlannerResult(plan=plan, items=items)

    # -- sections ----------------------------------------------------------

    def _read_destination_state(
        self, destination: MusicPlatformReadPort, job: TransferJob
    ) -> DestinationState:
        """Read destination state selectively, degrading safely to all-UNKNOWN when unsupported."""

        sections = content_sections(job.requested_content)
        try:
            return destination.get_destination_state(sections=sections)
        except UnsupportedCapabilityError:
            self._logger.info(
                "event=plan_destination_state_unsupported platform=%s destination_presence=unknown",
                job.destination_platform,
            )
            return DestinationState(platform=job.destination_platform)

    def _resolve_identifier(
        self,
        item: Any,
        spec: TransferContentSpec,
        destination: MusicPlatformReadPort,
        capabilities: PlatformCapabilities,
        source_platform: Platform,
    ) -> IdentifierResolution:
        """Resolve a destination identifier through direct reuse, search, or explicit failure."""

        source_id = str(getattr(item, "source_id", ""))
        entity_type = spec.entity_type

        # 1. Direct reusable identifier
        if destination.can_reuse_identifier(entity_type, source_platform):
            return IdentifierResolution(
                destination_id=source_id,
                match_method=MatchMethod.DIRECT_ID,
                match_score=1.0,
                outcome=MatchOutcome.MATCHED,
                reasons=("direct_identifier_reused",),
            )

        # 2. Destination catalog resolution if declared capability is supported
        if spec.search_capability and capabilities.supports(spec.search_capability):
            if entity_type is EntityType.TRACK:
                candidates = destination.search_track(item)
                match = self._matcher.match(item, candidates)
                return IdentifierResolution.from_match(match)
            if entity_type is EntityType.ALBUM:
                candidates = destination.search_album(item)
                match = self._album_matcher.match(item, candidates)
                return IdentifierResolution.from_match(match)
            if entity_type is EntityType.ARTIST:
                candidates = destination.search_artist(item)
                match = self._artist_matcher.match(item, candidates)
                return IdentifierResolution.from_match(match)

        # 3. Explicit NOT_FOUND / unsupported resolution
        return IdentifierResolution(
            destination_id=None,
            match_method=MatchMethod.NONE,
            match_score=0.0,
            outcome=MatchOutcome.NOT_FOUND,
            reasons=("destination_resolution_unavailable",),
        )

    def _plan_section(
        self,
        job: TransferJob,
        source: LibrarySnapshot,
        destination: MusicPlatformReadPort,
        capabilities: PlatformCapabilities,
        state: DestinationState | None,
        content_type: ContentType,
        warnings: list[str],
    ) -> list[TransferItem]:
        """Plan a set-like section (tracks, albums, artists)."""

        spec = require_transfer_content_spec(content_type)
        section = spec.snapshot_sections[0]
        if section in source.incomplete_sections:
            warnings.append(f"source_section_incomplete:{section}")
            self._logger.error(
                "event=plan_section_incomplete job_id=%s section=%s", job.id, section
            )
            return []
        for write_cap in spec.destination_write_capabilities:
            capabilities.require(write_cap)
        entity_type = spec.entity_type
        raw_items = list(getattr(source, section))
        ordered = self._order(job, raw_items, section)
        planned: list[TransferItem] = []
        for position, item in enumerate(ordered):
            source_id = str(getattr(item, "source_id", ""))
            if not source_id:
                continue
            transfer_item = TransferItem.create(
                job.id,
                entity_type,
                job.source_platform,
                source_id,
                job.destination_platform,
                original_position=position,
                source_metadata=self._metadata_for(item),
                operation=spec.operation,
            )

            resolution = self._resolve_identifier(
                item, spec, destination, capabilities, job.source_platform
            )
            transfer_item.match_method = resolution.match_method
            transfer_item.match_score = resolution.match_score
            transfer_item.destination_id = resolution.destination_id

            if resolution.outcome is MatchOutcome.MATCHED:
                transfer_item.status = ItemStatus.MATCHED
            elif resolution.outcome is MatchOutcome.AMBIGUOUS:
                transfer_item.status = ItemStatus.AMBIGUOUS
                transfer_item.last_error = "ambiguous"
            else:
                transfer_item.status = ItemStatus.NOT_FOUND
                if "destination_resolution_unavailable" in resolution.reasons:
                    transfer_item.last_error = "destination_resolution_unavailable"
                else:
                    transfer_item.last_error = "not_found"

            for warning in resolution.warnings:
                warnings.append(f"{warning}:{source_id}")

            if transfer_item.destination_id is not None:
                if state is not None:
                    presence = state.presence(entity_type, transfer_item.destination_id)
                else:
                    presence = DestinationPresence.UNKNOWN

                if job.settings.skip_already_existing:
                    if presence is DestinationPresence.PRESENT:
                        transfer_item.status = ItemStatus.ALREADY_EXISTS
                    elif presence is DestinationPresence.ABSENT:
                        if transfer_item.status is ItemStatus.PENDING:
                            transfer_item.status = ItemStatus.MATCHED
                    elif presence is DestinationPresence.UNKNOWN:
                        reason = "state_unsupported"
                        if state is not None:
                            if section in state.incomplete_sections:
                                reason = "section_incomplete"
                            elif section not in state.complete_sections:
                                reason = "section_not_read"
                        raise DestinationPresenceUnknownError(
                            f"destination_presence_unknown:{section}:{transfer_item.destination_id}",
                            section=section,
                            entity_type=entity_type,
                            destination_id=transfer_item.destination_id,
                            reason=reason,
                        )
                else:
                    if transfer_item.status is ItemStatus.PENDING:
                        transfer_item.status = ItemStatus.MATCHED
            planned.append(transfer_item)
        return planned


    def _plan_playlists(
        self,
        job: TransferJob,
        source: LibrarySnapshot,
        destination: MusicPlatformReadPort,
        capabilities: PlatformCapabilities,
        state: DestinationState | None,
        warnings: list[str],
    ) -> list[TransferItem]:
        """Plan playlists and every playlist item, preserving duplicates.

        Playlist content is sequence-like, never set-like: a playlist may
        intentionally contain the same track twice (Invariant D), so no global
        deduplication happens here.
        """

        if "playlists" in source.incomplete_sections:
            warnings.append("source_section_incomplete:playlists")
            return []
        spec = require_transfer_content_spec(ContentType.PLAYLISTS)
        for write_cap in spec.destination_write_capabilities:
            capabilities.require(write_cap)
        planned: list[TransferItem] = []
        for playlists_position, playlist in enumerate(source.playlists):
            playlist_item = TransferItem.create(
                job.id,
                EntityType.PLAYLIST,
                job.source_platform,
                playlist.source_id,
                job.destination_platform,
                original_position=playlists_position,
                source_metadata={
                    "name": playlist.name,
                    "description": playlist.description or "",
                    "track_count": playlist.track_count,
                },
                operation=TransferOperation.CREATE_PLAYLIST,
            )
            playlist_item.destination_id = playlist.source_id if destination.can_reuse_identifier(
                EntityType.PLAYLIST, job.source_platform
            ) else None
            playlist_item.match_method = _DIRECT_METHOD if playlist_item.destination_id else _NONE_METHOD
            playlist_item.match_score = 1.0 if playlist_item.destination_id else 0.0
            playlist_item.status = (
                ItemStatus.MATCHED if playlist_item.destination_id else ItemStatus.PENDING
            )
            planned.append(playlist_item)
            if not playlist.tracks:
                continue
            playlist_entries: list[TransferItem] = []
            for position, entry in enumerate(playlist.tracks):
                if entry.track is None:
                    warnings.append(f"playlist_item_unresolved:{playlist.source_id}:{position}")
                    continue
                planned_item = self._plan_playlist_item(
                    job, destination, playlist, entry, position, capabilities
                )
                playlist_entries.append(planned_item)

            write_pos = 0
            for item in playlist_entries:
                if item.operation is TransferOperation.ADD_PLAYLIST_ITEM and item.is_executable():
                    item.write_position = write_pos
                    write_pos += 1
                else:
                    item.write_position = None
            planned.extend(playlist_entries)
        return planned

    def _plan_playlist_item(
        self,
        job: TransferJob,
        destination: MusicPlatformReadPort,
        playlist: Playlist,
        entry: Any,
        position: int,
        capabilities: PlatformCapabilities,
    ) -> TransferItem:
        """Plan one playlist entry, keeping its position and duplicates."""

        track: Track = entry.track
        item = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            job.source_platform,
            track.source_id,
            job.destination_platform,
            original_position=position,
            container_source_id=playlist.source_id,
            source_metadata={
                "title": track.title,
                "artists": track.artist_names,
                "isrc": track.isrc,
                "duration_ms": track.duration_ms,
            },
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )

        if destination.can_reuse_identifier(EntityType.PLAYLIST_ITEM, job.source_platform):
            item.destination_id = track.source_id
            item.match_method = _DIRECT_METHOD
            item.match_score = 1.0
            item.status = ItemStatus.MATCHED
            return item
        if capabilities.supports("search_tracks"):
            match = self._matcher.match(track, destination.search_track(track))
            item.match_method = match.method
            item.match_score = match.score
            item.destination_id = match.destination_id
            item.status = {
                MatchOutcome.MATCHED: ItemStatus.MATCHED,
                MatchOutcome.AMBIGUOUS: ItemStatus.AMBIGUOUS,
                MatchOutcome.NOT_FOUND: ItemStatus.NOT_FOUND,
            }[match.outcome]
            if match.outcome is not MatchOutcome.MATCHED:
                item.last_error = match.outcome.value
        return item

    # -- helpers -----------------------------------------------------------

    def _order(self, job: TransferJob, items: list[Any], section: str) -> list[Any]:
        """Apply the requested logical order to one section."""

        if section == "tracks":
            return apply_logical_order(
                items,
                job.settings.ordering,
                date_added=lambda track: track.date_added,
                title=lambda track: track.title,
                artist=lambda track: (track.artist_names or [None])[0],
                album=lambda track: track.album_title,
            )
        if section == "albums":
            return apply_logical_order(
                items,
                job.settings.ordering,
                date_added=lambda album: album.release_date,
                title=lambda album: album.title,
                artist=lambda album: (album.artist_names or [None])[0],
                album=lambda album: album.title,
            )
        return apply_logical_order(
            items,
            job.settings.ordering,
            title=lambda record: getattr(record, "name", "") or getattr(record, "title", ""),
        )

    @staticmethod
    def _metadata_for(item: Any) -> dict[str, Any]:
        """Return compact source metadata for logs, reports, and review screens."""


        title = getattr(item, "title", "") or getattr(item, "name", "")
        metadata: dict[str, Any] = {"title": str(title)}
        artists = getattr(item, "artist_names", None)
        if callable(artists):
            metadata["artists"] = list(artists())
        isrc = getattr(item, "isrc", None)
        if isrc:
            metadata["isrc"] = str(isrc)
        return metadata

    @staticmethod
    def _summarize(
        items: list[TransferItem], source: LibrarySnapshot, state: DestinationState | None
    ) -> TransferPlanSummary:
        """Count planned items by status.

        :class:`TransferPlanSummary` is frozen, so the counts are tallied into
        locals and the summary is built once.  A plan is a value: nothing may
        mutate it after it has been shown to a user for confirmation.
        """

        counts = {
            ItemStatus.ALREADY_EXISTS: 0,
            ItemStatus.MATCHED: 0,
            ItemStatus.AMBIGUOUS: 0,
            ItemStatus.NOT_FOUND: 0,
            ItemStatus.SKIPPED: 0,
        }
        for item in items:
            if item.status in counts:
                counts[item.status] += 1
        return TransferPlanSummary(
            total_items=len(items),
            already_exists_items=counts[ItemStatus.ALREADY_EXISTS],
            matched_items=counts[ItemStatus.MATCHED],
            ambiguous_items=counts[ItemStatus.AMBIGUOUS],
            not_found_items=counts[ItemStatus.NOT_FOUND],
            skipped_items=counts[ItemStatus.SKIPPED],
            playlist_count=sum(
                1 for item in items if item.entity_type is EntityType.PLAYLIST
            ),
        )

    @staticmethod
    def _validate_plan(items: list[TransferItem]) -> None:
        """Validate playlist write positions and set-like executable invariants before plan finalization.

        Rules:
        1. Executable ADD_PLAYLIST_ITEM entries must have write_position is not None.
        2. Non-executable or non-playlist items must have write_position is None.
        3. For each playlist container, executable write positions must be unique
           and strictly contiguous 0..N-1.
        4. Every set-like item with executable status (PENDING or MATCHED) must have
           a non-empty destination_id.
        """
        validate_plan_write_positions(items)
        validate_plan_set_like_items(items)


def migrate_legacy_write_positions(items: list[TransferItem]) -> None:
    """Derive write positions for legacy items lacking write_position.

    If in a playlist container, every playlist item has write_position is None,
    we derive contiguous write positions (0..N-1) for executable or already
    transferred items in original_position order. Unresolved items (NOT_FOUND,
    UNAVAILABLE, etc.) remain write_position = None.
    """
    by_container: dict[str, list[TransferItem]] = {}
    for item in items:
        if item.entity_type is not EntityType.PLAYLIST_ITEM:
            continue
        container = item.container_source_id or ""
        by_container.setdefault(container, []).append(item)

    for _container_id, entries in by_container.items():
        if not any(e.write_position is not None for e in entries):
            sorted_entries = sorted(entries, key=lambda e: e.original_position)
            write_pos = 0
            for entry in sorted_entries:
                if (
                    entry.operation is TransferOperation.ADD_PLAYLIST_ITEM
                    or (entry.operation is TransferOperation.NONE and entry.destination_id)
                ) and (entry.is_executable() or entry.status is ItemStatus.TRANSFERRED):
                    entry.write_position = write_pos
                    write_pos += 1
                else:
                    entry.write_position = None


def validate_plan_write_positions(items: list[TransferItem]) -> None:
    """Validate write positions across all plan items.

    Raises:
        TransferConfigurationError: If any playlist container has invalid,
            duplicate, non-contiguous, or missing write positions for executable entries.
    """
    by_container: dict[str, list[TransferItem]] = {}
    for item in items:
        if item.entity_type is not EntityType.PLAYLIST_ITEM:
            continue
        container = item.container_source_id or ""
        by_container.setdefault(container, []).append(item)

    for container_id, entries in by_container.items():
        positioned_entries = [e for e in entries if e.write_position is not None]
        executable_entries = [
            e
            for e in entries
            if e.operation is TransferOperation.ADD_PLAYLIST_ITEM and e.is_executable()
        ]
        for e in executable_entries:
            if e.write_position is None:
                raise TransferConfigurationError(
                    f"invalid_playlist_write_positions:missing_write_position:{container_id}:{e.id}"
                )

        positions = [e.write_position for e in positioned_entries]
        if len(positions) != len(set(positions)):
            raise TransferConfigurationError(
                f"invalid_playlist_write_positions:duplicate_position:{container_id}"
            )
        if positions:
            expected_positions = list(range(len(positions)))
            if sorted(positions) != expected_positions:
                raise TransferConfigurationError(
                    f"invalid_playlist_write_positions:non_contiguous:{container_id}"
                )


def validate_plan_set_like_items(items: list[TransferItem]) -> None:
    """Validate that every executable set-like item has a non-empty destination identifier.

    Raises:
        TransferConfigurationError: If an executable set-like item is missing its destination ID.
    """
    for item in items:
        if (
            item.operation
            in (
                TransferOperation.SAVE_TRACK,
                TransferOperation.SAVE_ALBUM,
                TransferOperation.FOLLOW_ARTIST,
            )
            and item.status in (ItemStatus.PENDING, ItemStatus.MATCHED)
            and not item.destination_id
        ):
            raise TransferConfigurationError(
                f"unresolved_executable_item:{item.entity_type}:{item.source_id}"
            )


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
    LibraryRecord,
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
from ..ports import (
    DestinationState,
    MusicPlatformReadPort,
    PlatformCapabilities,
    destination_section_for_entity,
)
from .ordering import apply_logical_order

_LOGGER = logging.getLogger("music_transfer.planner")


#: Match methods recorded when no catalog search is required.
_DIRECT_METHOD = MatchMethod.DIRECT_ID
_NONE_METHOD = MatchMethod.NONE


@dataclass(frozen=True, slots=True)
class StructuralContentDependency:
    """A structural prerequisite section required by a content type (e.g. folders for playlists)."""

    snapshot_section: str
    source_read_capability: str
    destination_write_capability: str | None = None


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
    structural_dependencies: tuple[StructuralContentDependency, ...] = ()


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
        structural_dependencies=(
            StructuralContentDependency(
                snapshot_section="folders",
                source_read_capability="read_folders",
                destination_write_capability="create_folders",
            ),
        ),
    ),
    ContentType.VIDEOS: TransferContentSpec(
        content_type=ContentType.VIDEOS,
        snapshot_sections=("videos",),
        entity_type=EntityType.VIDEO,
        operation=TransferOperation.SAVE_VIDEO,
        resolution_policy=IdentifierResolutionPolicy.REUSE_ONLY,
        search_capability=None,
        source_read_capabilities=("read_videos",),
        destination_write_capabilities=("write_videos",),
    ),
    ContentType.MIXES: TransferContentSpec(
        content_type=ContentType.MIXES,
        snapshot_sections=("mixes",),
        entity_type=EntityType.MIX,
        operation=TransferOperation.SAVE_MIX,
        resolution_policy=IdentifierResolutionPolicy.REUSE_ONLY,
        search_capability=None,
        source_read_capabilities=("read_mixes",),
        destination_write_capabilities=("write_mixes",),
    ),
}

#: Backward-compatible alias
CONTENT_SPECS = ENGINE_TRANSFER_SPECS


def validate_transfer_content_spec(spec: TransferContentSpec) -> None:
    """Validate internal consistency of a TransferContentSpec.

    Raises:
        TransferConfigurationError: If the spec contains contradictory,
            missing, or unsupported configuration combinations.
    """
    if not isinstance(spec, TransferContentSpec):
        raise TransferConfigurationError(
            f"invalid_transfer_content_spec_type:{type(spec).__name__}"
        )

    for dep in spec.structural_dependencies:
        if not isinstance(dep, StructuralContentDependency):
            raise TransferConfigurationError(
                f"invalid_structural_dependency_type:{type(dep).__name__}"
            )
        if not dep.snapshot_section or not isinstance(dep.snapshot_section, str):
            raise TransferConfigurationError(
                f"invalid_structural_dependency_section:{spec.content_type.value}"
            )
        if not dep.source_read_capability or not isinstance(dep.source_read_capability, str):
            raise TransferConfigurationError(
                f"invalid_structural_dependency_source_capability:{spec.content_type.value}"
            )
        if dep.destination_write_capability is not None and not isinstance(
            dep.destination_write_capability, str
        ):
            raise TransferConfigurationError(
                f"invalid_structural_dependency_destination_capability:{spec.content_type.value}"
            )

    policy = spec.resolution_policy
    if policy is IdentifierResolutionPolicy.REUSE_OR_SEARCH:
        if not spec.search_capability or not isinstance(spec.search_capability, str):
            raise TransferConfigurationError(
                f"reuse_or_search_missing_search_capability:{spec.content_type.value}"
            )
    elif policy is IdentifierResolutionPolicy.REUSE_ONLY:
        if spec.search_capability is not None:
            raise TransferConfigurationError(
                f"reuse_only_unexpected_search_capability:{spec.content_type.value}"
            )
    elif policy is IdentifierResolutionPolicy.CONTAINER_CREATE:
        if spec.search_capability is not None:
            raise TransferConfigurationError(
                f"container_create_unexpected_search_capability:{spec.content_type.value}"
            )
    else:
        raise TransferConfigurationError(f"unrecognized_resolution_policy:{policy}")


for _registered_spec in ENGINE_TRANSFER_SPECS.values():
    validate_transfer_content_spec(_registered_spec)


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
    validate_transfer_content_spec(spec)
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


CANONICAL_SOURCE_EXPORT_ORDER: tuple[str, ...] = (
    "albums",
    "artists",
    "folders",
    "mixes",
    "playlists",
    "tracks",
    "videos",
)


def source_export_sections(
    content: tuple[ContentType, ...] | list[ContentType] | Iterable[ContentType],
    source_capabilities: PlatformCapabilities,
) -> tuple[str, ...]:
    """Return the snapshot sections required for source export, in canonical order.

    Includes primary content sections, and conditionally includes structural dependency
    sections (such as 'folders' for PLAYLISTS) if supported by the source platform.
    Sections are returned in canonical order: 'folders' precedes 'playlists'.
    """
    sections: set[str] = set()
    for item in content:
        spec = require_transfer_content_spec(item)
        sections.update(spec.snapshot_sections)
        for dep in spec.structural_dependencies:
            if source_capabilities.supports(dep.source_read_capability):
                sections.add(dep.snapshot_section)
    return tuple(s for s in CANONICAL_SOURCE_EXPORT_ORDER if s in sections)


def folder_parent_source_id(record: LibraryRecord) -> str | None:
    """Extract parent folder source ID from a folder record (None = root)."""
    parent = record.metadata.get("parent_source_id")
    if parent is None and "parent_id" in record.metadata:
        parent = record.metadata.get("parent_id")
    if parent is None:
        return None
    return str(parent)


def validate_folder_hierarchy(
    folders: list[LibraryRecord],
    playlists: list[Playlist],
) -> list[LibraryRecord]:
    """Validate playlist folder hierarchy graph and return topologically sorted folders.

    Rules:
    1. Every folder source_id is present, non-empty, and unique.
    2. Every folder title/name is present and non-empty.
    3. Each folder parent is None (root) or an existing folder source_id.
    4. Folder cannot parent itself.
    5. Folder graph contains no cycles.
    6. Every non-root playlist.folder_id refers to an existing exported source folder.
    7. Normalized hierarchy is deterministic (topological ordering, parent before child).

    Raises:
        TransferConfigurationError: If any hierarchy validation rule is violated.
    """
    folder_map: dict[str, LibraryRecord] = {}
    folder_indices: dict[str, int] = {}
    for idx, f in enumerate(folders):
        if not f.source_id or not str(f.source_id).strip():
            reason = "missing_folder_id"
            raise TransferConfigurationError(f"invalid_folder_hierarchy:{reason}")
        if not f.title or not str(f.title).strip():
            reason = "missing_folder_name"
            raise TransferConfigurationError(f"invalid_folder_hierarchy:{reason}:{f.source_id}")
        fid = str(f.source_id)
        if fid in folder_map:
            raise TransferConfigurationError(f"invalid_folder_hierarchy:duplicate_folder_id:{fid}")
        folder_map[fid] = f
        folder_indices[fid] = idx

    # Validate parent existence and self-parenting
    for fid, f in folder_map.items():
        pid = folder_parent_source_id(f)
        if pid is not None:
            if pid == fid:
                raise TransferConfigurationError(f"invalid_folder_hierarchy:self_parent:{fid}")
            if pid not in folder_map:
                raise TransferConfigurationError(f"invalid_folder_hierarchy:missing_parent:{fid}:{pid}")

    # Cycle detection
    # 0 = unvisited, 1 = visiting (in current path), 2 = visited
    state: dict[str, int] = {}
    for fid in folder_map:
        if state.get(fid) == 2:
            continue
        curr: str | None = fid
        path: list[str] = []
        while curr is not None:
            curr_state = state.get(curr, 0)
            if curr_state == 1:
                cycle_start = path.index(curr)
                cycle_nodes = path[cycle_start:] + [curr]
                raise TransferConfigurationError(
                    f"invalid_folder_hierarchy:cycle_detected:{'->'.join(cycle_nodes)}"
                )
            if curr_state == 2:
                break
            state[curr] = 1
            path.append(curr)
            parent_rec = folder_map.get(curr)
            curr = folder_parent_source_id(parent_rec) if parent_rec else None

        for node in path:
            state[node] = 2

    # Validate playlist folder references
    for pl in playlists:
        pfid = pl.folder_id
        if pfid is None:
            continue
        if str(pfid) not in folder_map:
            raise TransferConfigurationError(
                f"invalid_folder_hierarchy:playlist_missing_folder:{pl.source_id}:{pfid}"
            )

    # Compute depth for deterministic topological sorting
    depths: dict[str, int] = {}

    def get_depth(node_id: str) -> int:
        if node_id in depths:
            return depths[node_id]
        parent_id = folder_parent_source_id(folder_map[node_id])
        depth = 0 if parent_id is None else get_depth(parent_id) + 1
        depths[node_id] = depth
        return depth

    for fid in folder_map:
        get_depth(fid)

    # Topological sort: lower depth first (root = 0, child = 1, etc.), tie-break by original index
    sorted_folders = sorted(
        folders,
        key=lambda rec: (depths[str(rec.source_id)], folder_indices[str(rec.source_id)]),
    )
    return sorted_folders


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
                playlist_item_type=it.playlist_item_type,
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
                    section = destination_section_for_entity(it.entity_type)
                    presence = state.presence_in_section(section, it.destination_id)
                except InvalidDestinationSectionError:
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

        policy = spec.resolution_policy

        if policy is IdentifierResolutionPolicy.REUSE_OR_SEARCH:
            # 1. Direct reusable identifier
            if destination.can_reuse_identifier(spec.entity_type, source_platform):
                return self._direct_reuse(item)
            # 2. Destination catalog resolution if declared capability is supported
            if spec.search_capability and capabilities.supports(spec.search_capability):
                return self._search_and_match(item, spec.entity_type, destination)
            # 3. Explicit NOT_FOUND / unsupported resolution
            return IdentifierResolution(
                destination_id=None,
                match_method=MatchMethod.NONE,
                match_score=0.0,
                outcome=MatchOutcome.NOT_FOUND,
                reasons=("destination_resolution_unavailable",),
            )

        if policy is IdentifierResolutionPolicy.REUSE_ONLY:
            # 1. Direct reusable identifier
            if destination.can_reuse_identifier(spec.entity_type, source_platform):
                return self._direct_reuse(item)
            # 2. Explicit NOT_FOUND without searching
            return IdentifierResolution(
                destination_id=None,
                match_method=MatchMethod.NONE,
                match_score=0.0,
                outcome=MatchOutcome.NOT_FOUND,
                reasons=("destination_resolution_unavailable",),
            )

        if policy is IdentifierResolutionPolicy.CONTAINER_CREATE:
            raise TransferConfigurationError(
                f"container_create_resolution_unsupported:{spec.content_type.value}"
            )

        raise TransferConfigurationError(
            f"unhandled_resolution_policy:{policy}"
        )

    def _direct_reuse(self, item: Any) -> IdentifierResolution:
        """Build a successful resolution reusing the source identifier."""
        source_id = str(getattr(item, "source_id", ""))
        return IdentifierResolution(
            destination_id=source_id,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            outcome=MatchOutcome.MATCHED,
            reasons=("direct_identifier_reused",),
        )

    def _search_and_match(
        self,
        item: Any,
        entity_type: EntityType,
        destination: MusicPlatformReadPort,
    ) -> IdentifierResolution:
        """Query destination catalog candidates and score matches."""
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
        raise TransferConfigurationError(f"unsupported_search_entity_type:{entity_type.value}")

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
        """Plan folders, playlists, and every playlist item, preserving duplicates and hierarchy.

        Playlist content is sequence-like, never set-like: a playlist may
        intentionally contain the same track twice (Invariant D), so no global
        deduplication happens here.
        """

        if "playlists" in source.incomplete_sections:
            warnings.append("source_section_incomplete:playlists")
            return []
        if "folders" in source.incomplete_sections:
            fld_section = "folders"
            raise TransferConfigurationError(f"source_section_incomplete:{fld_section}")

        spec = require_transfer_content_spec(ContentType.PLAYLISTS)
        for write_cap in spec.destination_write_capabilities:
            capabilities.require(write_cap)

        if source.folders:
            capabilities.require("create_folders")
            sorted_folders = validate_folder_hierarchy(source.folders, source.playlists)
        else:
            validate_folder_hierarchy([], source.playlists)
            sorted_folders = []

        planned: list[TransferItem] = []
        for folder_position, folder in enumerate(sorted_folders):
            parent_source_id = folder_parent_source_id(folder)
            folder_item = TransferItem.create(
                job.id,
                EntityType.FOLDER,
                job.source_platform,
                folder.source_id,
                job.destination_platform,
                original_position=folder_position,
                container_source_id=parent_source_id,
                source_metadata={
                    "name": folder.title,
                    "parent_source_id": parent_source_id,
                },
                operation=TransferOperation.CREATE_FOLDER,
            )
            folder_item.destination_id = None
            folder_item.container_destination_id = None
            folder_item.match_method = _NONE_METHOD
            folder_item.match_score = 0.0
            folder_item.status = ItemStatus.PENDING
            planned.append(folder_item)

        for playlists_position, playlist in enumerate(source.playlists):
            norm_folder_id = playlist.folder_id
            playlist_item = TransferItem.create(
                job.id,
                EntityType.PLAYLIST,
                job.source_platform,
                playlist.source_id,
                job.destination_platform,
                original_position=playlists_position,
                container_source_id=norm_folder_id,
                source_metadata={
                    "name": playlist.name,
                    "description": playlist.description or "",
                    "track_count": playlist.track_count,
                    "folder_id": norm_folder_id,
                },
                operation=TransferOperation.CREATE_PLAYLIST,
            )
            playlist_item.container_destination_id = None
            playlist_item.destination_id = (
                playlist.source_id
                if destination.can_reuse_identifier(EntityType.PLAYLIST, job.source_platform)
                else None
            )
            playlist_item.match_method = (
                _DIRECT_METHOD if playlist_item.destination_id else _NONE_METHOD
            )
            playlist_item.match_score = 1.0 if playlist_item.destination_id else 0.0
            playlist_item.status = (
                ItemStatus.MATCHED if playlist_item.destination_id else ItemStatus.PENDING
            )
            planned.append(playlist_item)
            if not playlist.tracks:
                continue
            playlist_entries: list[TransferItem] = []
            blocks_playlist = False
            for position, entry in enumerate(playlist.tracks):
                planned_item = self._plan_playlist_item(
                    job, destination, playlist, entry, position, capabilities
                )
                playlist_entries.append(planned_item)
                if not planned_item.is_executable():
                    err_code = planned_item.last_error or "playlist_item_unresolved"
                    warnings.append(f"{err_code}:{playlist.source_id}:{position}")
                    if planned_item.last_error in (
                        "playlist_item_unresolved",
                        "playlist_item_resolution_unsupported:video",
                        "playlist_item_resolution_unsupported:track",
                    ):
                        blocks_playlist = True

            if blocks_playlist:
                playlist_item.status = ItemStatus.AMBIGUOUS
                playlist_item.last_error = "playlist_sequence_unresolved"
                warnings.append(f"playlist_sequence_unresolved:{playlist.source_id}")
                for item in playlist_entries:
                    item.write_position = None
                    if item.is_executable():
                        item.status = ItemStatus.AMBIGUOUS
                        item.last_error = "playlist_sequence_unresolved"
            else:
                write_pos = 0
                for item in playlist_entries:
                    if (
                        item.operation is TransferOperation.ADD_PLAYLIST_ITEM
                        and item.is_executable()
                    ):
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
        """Plan one playlist entry, preserving exact media type, position, and duplicates."""

        if entry.track is not None:
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
                    "kind": "track",
                },
                operation=TransferOperation.ADD_PLAYLIST_ITEM,
                playlist_item_type=EntityType.TRACK,
            )
            if destination.can_reuse_identifier(EntityType.TRACK, job.source_platform):
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
            item.match_method = _NONE_METHOD
            item.match_score = 0.0
            item.destination_id = None
            item.status = ItemStatus.NOT_FOUND
            item.last_error = "playlist_item_resolution_unsupported:track"
            return item

        if entry.video is not None:
            video: LibraryRecord = entry.video
            source_meta = {"title": video.title, "kind": "video", **dict(video.metadata)}
            item = TransferItem.create(
                job.id,
                EntityType.PLAYLIST_ITEM,
                job.source_platform,
                video.source_id,
                job.destination_platform,
                original_position=position,
                container_source_id=playlist.source_id,
                source_metadata=source_meta,
                operation=TransferOperation.ADD_PLAYLIST_ITEM,
                playlist_item_type=EntityType.VIDEO,
            )
            if destination.can_reuse_identifier(EntityType.VIDEO, job.source_platform):
                item.destination_id = video.source_id
                item.match_method = _DIRECT_METHOD
                item.match_score = 1.0
                item.status = ItemStatus.MATCHED
                return item

            # Video must never use track search; no video search capability declared
            item.match_method = _NONE_METHOD
            item.match_score = 0.0
            item.destination_id = None
            item.status = ItemStatus.NOT_FOUND
            item.last_error = "playlist_item_resolution_unsupported:video"
            return item

        # Unresolved source occurrence: track is None and video is None
        occurrence_id = entry.source_item_id or f"{playlist.source_id}:pos:{position}"
        item = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            job.source_platform,
            occurrence_id,
            job.destination_platform,
            original_position=position,
            container_source_id=playlist.source_id,
            source_metadata=dict(entry.metadata),
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
            playlist_item_type=None,
        )
        item.match_method = _NONE_METHOD
        item.match_score = 0.0
        item.destination_id = None
        item.status = ItemStatus.NOT_FOUND
        item.last_error = "playlist_item_unresolved"
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
                TransferOperation.SAVE_VIDEO,
                TransferOperation.SAVE_MIX,
            )
            and item.status in (ItemStatus.PENDING, ItemStatus.MATCHED)
            and not item.destination_id
        ):
            raise TransferConfigurationError(
                f"unresolved_executable_item:{item.entity_type}:{item.source_id}"
            )


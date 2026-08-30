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
from dataclasses import dataclass
from typing import Any

from ..domain import (
    LibrarySnapshot,
    Playlist,
    Track,
    TransferItem,
    TransferJob,
    TransferPlan,
    TransferPlanSummary,
)
from ..enums import (
    ContentType,
    EntityType,
    ItemStatus,
    MatchMethod,
    MatchOutcome,
    TransferOperation,
)
from ..errors import UnsupportedCapabilityError
from ..matching import TrackMatcher
from ..ports import DestinationState, MusicPlatformAdapter, PlatformCapabilities, ReadOnlyAdapter
from .ordering import apply_logical_order

_LOGGER = logging.getLogger("music_transfer.planner")

#: Match methods recorded when no catalog search is required.
_DIRECT_METHOD = MatchMethod.DIRECT_ID
_NONE_METHOD = MatchMethod.NONE

#: Which library section, entity type, and capabilities each content type needs.
CONTENT_SPECS: dict[ContentType, dict[str, str]] = {
    ContentType.LIKED_TRACKS: {
        "section": "tracks",
        "entity_type": EntityType.TRACK.value,
        "read_capability": "read_liked_tracks",
        "write_capability": "write_liked_tracks",
    },
    ContentType.SAVED_ALBUMS: {
        "section": "albums",
        "entity_type": EntityType.ALBUM.value,
        "read_capability": "read_saved_albums",
        "write_capability": "write_saved_albums",
    },
    ContentType.FOLLOWED_ARTISTS: {
        "section": "artists",
        "entity_type": EntityType.ARTIST.value,
        "read_capability": "read_followed_artists",
        "write_capability": "write_followed_artists",
    },
    ContentType.PLAYLISTS: {
        "section": "playlists",
        "entity_type": EntityType.PLAYLIST.value,
        "read_capability": "read_playlists",
        "write_capability": "create_playlists",
    },
}


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

    def __init__(self, matcher: TrackMatcher | None = None, logger: logging.Logger | None = None) -> None:
        self._matcher = matcher or TrackMatcher()
        self._logger = logger or _LOGGER

    @property
    def matcher(self) -> TrackMatcher:
        """Return the matcher used for cross-platform catalog resolution."""

        return self._matcher

    def build(
        self,
        job: TransferJob,
        source: LibrarySnapshot,
        destination: MusicPlatformAdapter,
        *,
        destination_state: DestinationState | None = None,
    ) -> PlannerResult:
        """Produce a plan for one job.

        Args:
            job: The job being planned.
            source: The already-exported source snapshot.
            destination: The destination adapter.  It is wrapped in a
                read-only guard before any method is called.
            destination_state: A previously captured destination state.  When
                omitted, the planner reads it (still read-only).

        Raises:
            UnsupportedCapabilityError: If the destination cannot perform a
                requested write, or if the source snapshot is missing a
                requested section.
        """

        read_only = ReadOnlyAdapter(destination)
        capabilities = destination.capabilities
        state = destination_state
        if state is None:
            state = self._read_destination_state(read_only, job)
        items: list[TransferItem] = []
        warnings: list[str] = []
        for content_type in job.requested_content:
            if content_type is ContentType.PLAYLISTS:
                items.extend(
                    self._plan_playlists(job, source, read_only, capabilities, state, warnings)
                )
                continue
            items.extend(
                self._plan_section(job, source, read_only, capabilities, state, content_type, warnings)
            )
        summary = self._summarize(items, source, state)
        plan = TransferPlan(
            job_id=job.id,
            source_platform=job.source_platform,
            destination_platform=job.destination_platform,
            items=items,
            summary=summary,
            warnings=warnings,
            source_incomplete=source.is_partial,
            metadata={
                "destination_state": state.as_dict() if state is not None else None,
                "ordering": str(job.settings.ordering),
            },
        )
        self._logger.info(
            "event=plan_built job_id=%s items=%d matched=%d ambiguous=%d not_found=%d",
            job.id,
            summary.total_items,
            summary.matched_items,
            summary.ambiguous_items,
            summary.not_found_items,
        )
        return PlannerResult(plan=plan, items=items)

    # -- sections ----------------------------------------------------------

    def _read_destination_state(
        self, destination: MusicPlatformAdapter, job: TransferJob
    ) -> DestinationState:
        """Read destination state, degrading safely when unsupported."""

        try:
            return destination.get_destination_state()
        except UnsupportedCapabilityError:
            self._logger.info(
                "event=plan_destination_state_unsupported platform=%s",
                job.destination_platform,
            )
            return DestinationState(platform=job.destination_platform)

    def _plan_section(
        self,
        job: TransferJob,
        source: LibrarySnapshot,
        destination: MusicPlatformAdapter,
        capabilities: PlatformCapabilities,
        state: DestinationState | None,
        content_type: ContentType,
        warnings: list[str],
    ) -> list[TransferItem]:
        """Plan a set-like section (tracks, albums, artists)."""

        spec = CONTENT_SPECS[content_type]
        section = spec["section"]
        if section in source.incomplete_sections:
            warnings.append(f"source_section_incomplete:{section}")
            self._logger.error(
                "event=plan_section_incomplete job_id=%s section=%s", job.id, section
            )
            return []
        capabilities.require(spec["write_capability"])
        entity_type = EntityType(spec["entity_type"])
        raw_items = list(getattr(source, section))
        ordered = self._order(job, raw_items, section)
        planned: list[TransferItem] = []
        op = {
            EntityType.TRACK: TransferOperation.SAVE_TRACK,
            EntityType.ALBUM: TransferOperation.SAVE_ALBUM,
            EntityType.ARTIST: TransferOperation.FOLLOW_ARTIST,
        }.get(entity_type, TransferOperation.NONE)
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
                operation=op,
            )

            reusable = None
            if entity_type is EntityType.TRACK and destination.can_reuse_identifier(
                entity_type, job.source_platform
            ):
                reusable = source_id
                transfer_item.destination_id = source_id
                transfer_item.match_method = _DIRECT_METHOD
                transfer_item.match_score = 1.0
            elif entity_type is EntityType.TRACK and capabilities.supports("search_tracks"):
                match = self._matcher.match(item, destination.search_track(item))
                transfer_item.match_method = match.method
                transfer_item.match_score = match.score
                transfer_item.destination_id = match.destination_id
                if match.outcome is MatchOutcome.MATCHED:
                    transfer_item.status = ItemStatus.MATCHED
                else:
                    transfer_item.status = (
                        ItemStatus.AMBIGUOUS
                        if match.outcome is MatchOutcome.AMBIGUOUS
                        else ItemStatus.NOT_FOUND
                    )
                    transfer_item.last_error = match.outcome.value
                for warning in match.warnings:
                    warnings.append(f"{warning}:{source_id}")
            else:
                reusable = source_id if destination.can_reuse_identifier(
                    entity_type, job.source_platform
                ) else None
                if reusable is not None:
                    transfer_item.destination_id = reusable
                    transfer_item.match_method = _DIRECT_METHOD
                    transfer_item.match_score = 1.0
            if (
                job.settings.skip_already_existing
                and transfer_item.destination_id is not None
                and state is not None
                and self._state_contains(state, entity_type, transfer_item.destination_id)
            ):
                transfer_item.status = ItemStatus.ALREADY_EXISTS
            elif transfer_item.status is ItemStatus.PENDING and transfer_item.destination_id:
                transfer_item.status = ItemStatus.MATCHED
            planned.append(transfer_item)
        return planned

    def _plan_playlists(
        self,
        job: TransferJob,
        source: LibrarySnapshot,
        destination: MusicPlatformAdapter,
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
        capabilities.require("create_playlists")
        capabilities.require("write_playlist_items")
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
            if not capabilities.supports_playlist_duplicates:
                warnings.append(f"destination_deduplicates_playlists:{playlist.source_id}")
            for position, entry in enumerate(playlist.tracks):
                if entry.track is None:
                    warnings.append(f"playlist_item_unresolved:{playlist.source_id}:{position}")
                    continue
                planned.append(
                    self._plan_playlist_item(
                        job, destination, playlist, entry, position, capabilities
                    )
                )
        return planned

    def _plan_playlist_item(
        self,
        job: TransferJob,
        destination: MusicPlatformAdapter,
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
    def _state_contains(
        state: DestinationState, entity_type: EntityType, identifier: str
    ) -> bool:
        """Return whether the destination already holds an identifier."""

        if entity_type is EntityType.TRACK:
            return state.has_track(identifier)
        if entity_type is EntityType.ALBUM:
            return identifier in state.album_ids
        if entity_type is EntityType.ARTIST:
            return identifier in state.artist_ids
        if entity_type is EntityType.PLAYLIST:
            return identifier in state.playlist_ids
        return False

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

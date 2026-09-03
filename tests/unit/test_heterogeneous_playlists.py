"""Unit and integration tests for Phase 1.5D: Heterogeneous Playlist Media Items.

Covers:
1. PlaylistItem domain model: mutual exclusion, helper properties, serialization, legacy migration.
2. PlaylistMediaRef: immutability, canonical tokens, serialization.
3. TIDAL mapper: mapping tracks and videos to PlaylistItem without track mapper conflation.
4. TransferItem / TransferPlanItem: playlist_item_type serialization, backward compatibility, plan hash integrity.
5. TransferPlanner: typed TRACK/VIDEO resolution, no search_track for video, no silent dropping of items,
   fail-closed playlist sequence blocking, sibling independence, empty playlist validity.
6. TransferExecutor: typed append calls, typed prefix reconciliation, crash recovery.
7. TransferVerifier: exact typed sequence comparison, type mismatch detection, canonical token reporting.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from music_transfer.app.services import TransferService
from music_transfer.core.domain import (
    LibraryRecord,
    Playlist,
    PlaylistItem,
    PlaylistMediaRef,
    TransferItem,
    TransferJob,
    TransferPlan,
    TransferPlanItem,
)
from music_transfer.core.enums import (
    ContentType,
    EntityType,
    ItemStatus,
    JobStatus,
    MatchMethod,
    MutationState,
    Platform,
    TransferOperation,
)
from music_transfer.core.errors import (
    AmbiguousOperationError,
    InvalidPersistedStateError,
)
from music_transfer.core.transfer.executor import TransferExecutor
from music_transfer.core.transfer.verifier import TransferVerifier, compare_sequences
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
)
from music_transfer.platforms.tidal.adapter import TidalAdapter
from music_transfer.platforms.tidal.client import TidalClientError, TidalLibraryClient
from music_transfer.platforms.tidal.mapper import playlist_item_from_tidal

from tests.support import FakePlatformAdapter, track


def build_service(root: Path) -> TransferService:
    return TransferService(
        JsonTransferJobRepository(root), JsonTransferItemRepository(root)
    )


class FakeTidalVideo:
    """Mock object whose type contains 'video' for is_video() check."""

    def __init__(
        self,
        identifier: str,
        title: str,
        duration: int = 240,
        release_date: str = "2024-01-01",
    ) -> None:
        self.id = identifier
        self.name = title
        self.duration = duration
        self.release_date = release_date
        self.artists: list[Any] = []


class PlaylistItemModelTests(unittest.TestCase):
    """Domain model tests for PlaylistItem and PlaylistMediaRef."""

    def test_playlist_item_mutual_exclusion(self) -> None:
        """Providing both track and video raises ValueError."""
        t = track("Track A", identifier="t1")
        v = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="v1",
            title="Video A",
        )
        with self.assertRaises(ValueError) as ctx:
            PlaylistItem(position=1, track=t, video=v)
        self.assertEqual(str(ctx.exception), "playlist_item_multiple_payloads")

    def test_playlist_item_track_properties(self) -> None:
        """Track playlist item provides correct helper properties."""
        t = track("Track A", identifier="t1")
        item = PlaylistItem(position=1, track=t)
        self.assertEqual(item.media_entity_type, EntityType.TRACK)
        self.assertEqual(item.media_id, "t1")
        self.assertEqual(item.track_id, "t1")

    def test_playlist_item_video_properties(self) -> None:
        """Video playlist item provides correct helper properties."""
        v = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="v1",
            title="Video A",
        )
        item = PlaylistItem(position=1, video=v)
        self.assertEqual(item.media_entity_type, EntityType.VIDEO)
        self.assertEqual(item.media_id, "v1")
        self.assertIsNone(item.track_id)

    def test_playlist_item_unresolved_properties(self) -> None:
        """Unresolved item provides None for helper properties."""
        item = PlaylistItem(position=1, source_item_id="item-1")
        self.assertIsNone(item.media_entity_type)
        self.assertIsNone(item.media_id)
        self.assertIsNone(item.track_id)

    def test_playlist_item_serialization_round_trip(self) -> None:
        """PlaylistItem round-trips via as_dict and from_dict for both track and video."""
        t = track("Track A", identifier="t1")
        v = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="v1",
            title="Video A",
            metadata={"duration": 180},
        )
        item_track = PlaylistItem(position=1, track=t)
        item_video = PlaylistItem(position=2, video=v)

        restored_track = PlaylistItem.from_dict(item_track.as_dict())
        self.assertEqual(restored_track.position, 1)
        self.assertIsNotNone(restored_track.track)
        self.assertIsNone(restored_track.video)
        self.assertEqual(restored_track.media_id, "t1")

        restored_video = PlaylistItem.from_dict(item_video.as_dict())
        self.assertEqual(restored_video.position, 2)
        self.assertIsNone(restored_video.track)
        self.assertIsNotNone(restored_video.video)
        self.assertEqual(restored_video.media_id, "v1")

    def test_playlist_item_legacy_deserialization_video(self) -> None:
        """Legacy dictionary with track holding kind='video' normalizes to video field."""
        legacy_dict = {
            "position": 1,
            "track": {
                "source_platform": "tidal",
                "source_id": "v-100",
                "title": "Music Video",
                "artist_names": ["Artist A"],
                "duration_ms": 210000,
                "metadata": {"kind": "video"},
            },
            "source_item_id": "si-1",
            "metadata": {"kind": "video"},
        }
        item = PlaylistItem.from_dict(legacy_dict)
        self.assertIsNone(item.track)
        self.assertIsNotNone(item.video)
        self.assertEqual(item.video.source_id, "v-100")
        self.assertEqual(item.video.title, "Music Video")
        self.assertEqual(item.media_entity_type, EntityType.VIDEO)
        self.assertEqual(item.media_id, "v-100")

    def test_playlist_media_ref_basics(self) -> None:
        """PlaylistMediaRef validates empty id, supports canonical_token, and serializes."""
        with self.assertRaises(ValueError) as ctx:
            PlaylistMediaRef(EntityType.TRACK, "")
        self.assertEqual(str(ctx.exception), "playlist_media_id_missing")

        ref_track = PlaylistMediaRef(EntityType.TRACK, "t-1")
        ref_video = PlaylistMediaRef(EntityType.VIDEO, "v-2")
        self.assertEqual(ref_track.canonical_token(), "track:t-1")
        self.assertEqual(ref_video.canonical_token(), "video:v-2")

        self.assertEqual(PlaylistMediaRef.from_dict(ref_track.as_dict()), ref_track)
        self.assertEqual(PlaylistMediaRef.from_dict(ref_video.as_dict()), ref_video)

    def test_playlist_ordered_media_refs(self) -> None:
        """Playlist.ordered_media_refs returns exact sequence of media refs."""
        t = track("Track 1", identifier="t1")
        v = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="v1",
            title="Video 1",
        )
        pl = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl1",
            name="Mix Playlist",
            tracks=[
                PlaylistItem(position=1, track=t),
                PlaylistItem(position=2, video=v),
                PlaylistItem(position=3, track=t),
            ],
        )
        refs = pl.ordered_media_refs()
        self.assertEqual(
            refs,
            [
                PlaylistMediaRef(EntityType.TRACK, "t1"),
                PlaylistMediaRef(EntityType.VIDEO, "v1"),
                PlaylistMediaRef(EntityType.TRACK, "t1"),
            ],
        )


class TidalMapperHeterogeneousTests(unittest.TestCase):
    """TIDAL mapper tests for heterogeneous playlist items."""

    def test_playlist_item_from_tidal_track(self) -> None:
        """Track payload maps to PlaylistItem with track."""
        track_obj = MagicMock()
        track_obj.id = "12345"
        track_obj.name = "Track Title"
        artist = MagicMock()
        artist.name = "Artist Name"
        track_obj.artists = [artist]
        track_obj.album = MagicMock()
        track_obj.album.name = "Album Name"
        track_obj.isrc = "US1234567890"
        track_obj.duration = 200

        item = playlist_item_from_tidal(track_obj, position=1)
        self.assertIsNotNone(item.track)
        self.assertIsNone(item.video)
        self.assertEqual(item.track.source_id, "12345")
        self.assertEqual(item.media_entity_type, EntityType.TRACK)

    @patch("music_transfer.platforms.tidal.mapper.track_from_tidal")
    def test_playlist_item_from_tidal_video(self, mock_track_from_tidal: MagicMock) -> None:
        """Video payload maps to PlaylistItem with LibraryRecord and never calls track_from_tidal."""
        video_obj = FakeTidalVideo("98765", "Video Title", duration=240)

        item = playlist_item_from_tidal(video_obj, position=2)
        mock_track_from_tidal.assert_not_called()
        self.assertIsNone(item.track)
        self.assertIsNotNone(item.video)
        self.assertEqual(item.video.source_id, "98765")
        self.assertEqual(item.video.title, "Video Title")
        self.assertEqual(item.metadata.get("kind"), "video")
        self.assertEqual(item.video.metadata.get("duration_seconds"), 240)
        self.assertEqual(item.media_entity_type, EntityType.VIDEO)
        self.assertEqual(item.media_id, "98765")


class PlanIdentityAndCompatibilityTests(unittest.TestCase):
    """Plan hash and persistence compatibility tests."""

    def test_plan_hash_changes_between_track_and_video(self) -> None:
        """Plan hash is distinct when an item's type changes from TRACK to VIDEO with same ID."""
        item_track = TransferPlanItem(
            entity_type=EntityType.PLAYLIST_ITEM,
            source_id="12345",
            destination_id="12345",
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            original_position=0,
            write_position=0,
            playlist_item_type=EntityType.TRACK,
        )
        item_video = TransferPlanItem(
            entity_type=EntityType.PLAYLIST_ITEM,
            source_id="12345",
            destination_id="12345",
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            original_position=0,
            write_position=0,
            playlist_item_type=EntityType.VIDEO,
        )

        plan_track = TransferPlan.create("job-1", revision=1, items=(item_track,))
        plan_video = TransferPlan.create("job-1", revision=1, items=(item_video,))

        self.assertNotEqual(plan_track.compute_hash(), plan_video.compute_hash())
        self.assertNotEqual(plan_track.plan_hash, plan_video.plan_hash)

    def test_legacy_plan_item_deserialization_normalizes_to_track(self) -> None:
        """Legacy serialized plan item with missing playlist_item_type defaults to TRACK."""
        legacy_dict: dict[str, Any] = {
            "entity_type": "playlist_item",
            "source_id": "track-10",
            "destination_id": "dst-track-10",
            "operation": "add_playlist_item",
            "planned_status": "matched",
            "match_method": "direct_id",
            "match_score": 1.0,
            "original_position": 0,
            "write_position": 0,
            "source_metadata": {"title": "Song"},
        }
        item = TransferPlanItem.from_dict(legacy_dict)
        self.assertEqual(item.playlist_item_type, EntityType.TRACK)

    def test_explicit_unknown_playlist_item_type_fails_closed(self) -> None:
        """Explicit invalid playlist_item_type fails closed with InvalidPersistedStateError."""
        bad_dict = {
            "entity_type": "playlist_item",
            "source_id": "item-1",
            "destination_id": "dst-item-1",
            "operation": "add_playlist_item",
            "planned_status": "matched",
            "match_method": "direct_id",
            "match_score": 1.0,
            "playlist_item_type": "garbage",
        }
        with self.assertRaises(InvalidPersistedStateError):
            TransferPlanItem.from_dict(bad_dict)
        with self.assertRaises(InvalidPersistedStateError):
            TransferItem.from_dict(bad_dict)

    def test_explicit_album_playlist_item_type_fails_closed(self) -> None:
        """Explicit album playlist_item_type fails closed with InvalidPersistedStateError."""
        album_dict = {
            "entity_type": "playlist_item",
            "source_id": "item-1",
            "destination_id": "dst-item-1",
            "operation": "add_playlist_item",
            "planned_status": "matched",
            "match_method": "direct_id",
            "match_score": 1.0,
            "playlist_item_type": "album",
        }
        with self.assertRaises(InvalidPersistedStateError):
            TransferPlanItem.from_dict(album_dict)
        with self.assertRaises(InvalidPersistedStateError):
            TransferItem.from_dict(album_dict)

    def test_missing_legacy_playlist_item_type_with_video_metadata_normalizes_to_video(self) -> None:
        """Legacy dictionary with missing playlist_item_type and kind='video' normalizes to video."""
        video_dict = {
            "id": "item-1",
            "job_id": "job-1",
            "entity_type": "playlist_item",
            "source_platform": "tidal",
            "source_id": "video-1",
            "destination_platform": "tidal",
            "destination_id": "dst-video-1",
            "operation": "add_playlist_item",
            "planned_status": "matched",
            "match_method": "direct_id",
            "match_score": 1.0,
            "source_metadata": {"kind": "video", "title": "A Video"},
        }
        item = TransferItem.from_dict(video_dict)
        self.assertEqual(item.playlist_item_type, EntityType.VIDEO)
        plan_item = TransferPlanItem.from_dict(video_dict)
        self.assertEqual(plan_item.playlist_item_type, EntityType.VIDEO)

    def test_missing_legacy_playlist_item_type_with_clear_track_metadata_normalizes_to_track(self) -> None:
        """Legacy dictionary with missing playlist_item_type and track metadata normalizes to track."""
        track_dict = {
            "id": "item-2",
            "job_id": "job-1",
            "entity_type": "playlist_item",
            "source_platform": "tidal",
            "source_id": "track-1",
            "destination_platform": "tidal",
            "destination_id": "dst-track-1",
            "operation": "add_playlist_item",
            "planned_status": "matched",
            "match_method": "direct_id",
            "match_score": 1.0,
            "source_metadata": {"isrc": "US123", "artists": ["Artist A"]},
        }
        item = TransferItem.from_dict(track_dict)
        self.assertEqual(item.playlist_item_type, EntityType.TRACK)
        plan_item = TransferPlanItem.from_dict(track_dict)
        self.assertEqual(plan_item.playlist_item_type, EntityType.TRACK)

    def test_transfer_plan_item_rejects_invalid_playlist_item_type(self) -> None:
        """TransferPlanItem constructor rejects invalid or non-playlist_item entity types."""
        with self.assertRaises(InvalidPersistedStateError):
            TransferPlanItem(
                entity_type=EntityType.PLAYLIST_ITEM,
                source_id="src-1",
                destination_id="dst-1",
                operation=TransferOperation.ADD_PLAYLIST_ITEM,
                planned_status=ItemStatus.MATCHED,
                match_method=MatchMethod.DIRECT_ID,
                match_score=1.0,
                playlist_item_type=EntityType.ALBUM,
            )
        with self.assertRaises(InvalidPersistedStateError):
            TransferPlanItem(
                entity_type=EntityType.TRACK,
                source_id="src-1",
                destination_id="dst-1",
                operation=TransferOperation.SAVE_TRACK,
                planned_status=ItemStatus.MATCHED,
                match_method=MatchMethod.DIRECT_ID,
                match_score=1.0,
                playlist_item_type=EntityType.TRACK,
            )


class HeterogeneousPlannerTests(unittest.TestCase):
    """Planner tests for heterogeneous playlist items and fail-closed sequence semantics."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self.tmp.name))
        self.job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_track_and_video_use_correct_entity_types_for_reuse(self) -> None:
        """Planner queries can_reuse_identifier with TRACK and VIDEO, never PLAYLIST_ITEM."""
        t = track("Track 1", identifier="t1")
        v = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="v1",
            title="Video 1",
        )
        pl = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl1",
            name="Mix",
            tracks=[PlaylistItem(position=1, track=t), PlaylistItem(position=2, video=v)],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl])
        destination = FakePlatformAdapter(display_name="destination")

        reused_entities: list[EntityType] = []
        original_reuse = destination.can_reuse_identifier

        def tracking_reuse(entity_type: EntityType, source_platform: Platform) -> bool:
            reused_entities.append(entity_type)
            return original_reuse(entity_type, source_platform)

        destination.can_reuse_identifier = tracking_reuse  # type: ignore[method-assign]

        self.service.analyze(self.job, source, destination)
        plan = self.service.plans.get_by_id(self.job.active_plan_id)
        assert plan is not None

        self.assertNotIn(EntityType.PLAYLIST_ITEM, reused_entities)
        self.assertIn(EntityType.TRACK, reused_entities)
        self.assertIn(EntityType.VIDEO, reused_entities)

        pl_items = [it for it in plan.items if it.entity_type is EntityType.PLAYLIST_ITEM]
        self.assertEqual(len(pl_items), 2)
        self.assertEqual(pl_items[0].playlist_item_type, EntityType.TRACK)
        self.assertEqual(pl_items[1].playlist_item_type, EntityType.VIDEO)
        self.assertEqual(pl_items[0].write_position, 0)
        self.assertEqual(pl_items[1].write_position, 1)

    def test_video_never_calls_search_track(self) -> None:
        """Unsupported video never calls destination.search_track."""
        v = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="v1",
            title="Video 1",
        )
        pl = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl1",
            name="Video Playlist",
            tracks=[PlaylistItem(position=1, video=v)],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl])
        destination = FakePlatformAdapter(display_name="destination")
        # Cannot reuse VIDEO
        destination.can_reuse_identifier = lambda et, sp: et != EntityType.VIDEO

        destination.search_track = MagicMock()  # type: ignore[method-assign]

        self.service.analyze(self.job, source, destination)
        destination.search_track.assert_not_called()

        plan = self.service.plans.get_by_id(self.job.active_plan_id)
        assert plan is not None

        video_items = [it for it in plan.items if it.entity_type is EntityType.PLAYLIST_ITEM]
        self.assertEqual(len(video_items), 1)
        self.assertEqual(video_items[0].planned_status, ItemStatus.NOT_FOUND)

    def test_unresolved_source_occurrence_blocks_entire_playlist(self) -> None:
        """Unresolved source occurrence is preserved in plan and blocks the playlist."""
        t = track("Track 1", identifier="t1")
        unresolved = PlaylistItem(position=2, source_item_id="unknown-item")
        pl = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl1",
            name="Broken Playlist",
            tracks=[PlaylistItem(position=1, track=t), unresolved],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl])
        destination = FakePlatformAdapter(display_name="destination")

        self.service.analyze(self.job, source, destination)
        plan = self.service.plans.get_by_id(self.job.active_plan_id)
        assert plan is not None

        pl_container = next(it for it in plan.items if it.entity_type is EntityType.PLAYLIST)
        self.assertEqual(pl_container.planned_status, ItemStatus.AMBIGUOUS)

        pl_items = [it for it in plan.items if it.entity_type is EntityType.PLAYLIST_ITEM]
        self.assertEqual(len(pl_items), 2)
        # Both child items must have write_position = None (non-executable)
        self.assertIsNone(pl_items[0].write_position)
        self.assertIsNone(pl_items[1].write_position)
        self.assertIn("playlist_sequence_unresolved:pl1", plan.warnings)

    def test_cross_platform_unsupported_video_blocks_entire_playlist(self) -> None:
        """Cross-platform destination with unsupported video blocks the playlist."""
        t = track("Track 1", identifier="t1")
        v = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="v1",
            title="Video 1",
        )
        pl = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl1",
            name="Mixed",
            tracks=[PlaylistItem(position=1, track=t), PlaylistItem(position=2, video=v)],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl])
        destination = FakePlatformAdapter(display_name="destination")
        destination.can_reuse_identifier = lambda et, sp: et != EntityType.VIDEO

        self.service.analyze(self.job, source, destination)
        plan = self.service.plans.get_by_id(self.job.active_plan_id)
        assert plan is not None

        pl_container = next(it for it in plan.items if it.entity_type is EntityType.PLAYLIST)
        self.assertEqual(pl_container.planned_status, ItemStatus.AMBIGUOUS)

        pl_items = [it for it in plan.items if it.entity_type is EntityType.PLAYLIST_ITEM]
        self.assertEqual(len(pl_items), 2)
        self.assertIsNone(pl_items[0].write_position)
        self.assertIsNone(pl_items[1].write_position)

    def test_one_blocked_playlist_does_not_block_sibling_playlist(self) -> None:
        """A blocked playlist does not prevent a valid sibling playlist from being transferred."""
        v = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="v1",
            title="Video 1",
        )
        pl_bad = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl-bad",
            name="Bad Playlist",
            tracks=[PlaylistItem(position=1, video=v)],
        )
        t2 = track("Track 2", identifier="t2")
        pl_good = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl-good",
            name="Good Playlist",
            tracks=[PlaylistItem(position=1, track=t2)],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl_bad, pl_good])
        destination = FakePlatformAdapter(display_name="destination")
        destination.can_reuse_identifier = lambda et, sp: et != EntityType.VIDEO

        self.service.analyze(self.job, source, destination)
        plan = self.service.plans.get_by_id(self.job.active_plan_id)
        assert plan is not None

        bad_container = next(
            it for it in plan.items if it.entity_type is EntityType.PLAYLIST and it.source_id == "pl-bad"
        )
        good_container = next(
            it for it in plan.items if it.entity_type is EntityType.PLAYLIST and it.source_id == "pl-good"
        )

        self.assertEqual(bad_container.planned_status, ItemStatus.AMBIGUOUS)
        self.assertEqual(good_container.planned_status, ItemStatus.MATCHED)

        good_items = [
            it for it in plan.items if it.container_source_id == "pl-good" and it.entity_type is EntityType.PLAYLIST_ITEM
        ]
        self.assertEqual(len(good_items), 1)
        self.assertEqual(good_items[0].planned_status, ItemStatus.MATCHED)
        self.assertEqual(good_items[0].write_position, 0)

    def test_empty_playlist_remains_valid(self) -> None:
        """An empty playlist is planned as executable and not blocked."""
        pl_empty = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl-empty",
            name="Empty Playlist",
            tracks=[],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl_empty])
        destination = FakePlatformAdapter(display_name="destination")

        self.service.analyze(self.job, source, destination)
        plan = self.service.plans.get_by_id(self.job.active_plan_id)
        assert plan is not None

        container = next(it for it in plan.items if it.entity_type is EntityType.PLAYLIST)
        self.assertEqual(container.planned_status, ItemStatus.PENDING)
        self.assertEqual(plan.warnings, [])

    def test_track_not_found_blocks_entire_playlist(self) -> None:
        """TRACK -> NOT_FOUND blocks entire parent playlist sequence."""
        t_a = track("Track A", identifier="a")
        t_b = track("Track B", identifier="b")
        v_v = LibraryRecord(source_platform=Platform.TIDAL, source_id="v", title="Video V")
        pl = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl1",
            name="Mix",
            tracks=[
                PlaylistItem(position=1, track=t_a),
                PlaylistItem(position=2, track=t_b),
                PlaylistItem(position=3, video=v_v),
            ],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl])
        # Destination only has Track A and Video V, so Track B is NOT_FOUND
        destination = FakePlatformAdapter(
            display_name="destination",
            tracks=[track("Track A", identifier="dst-a")],
            videos=[LibraryRecord(source_platform=Platform.TIDAL, source_id="v", title="Video V")],
        )
        destination.can_reuse_identifier = lambda et, sp: False

        self.service.analyze(self.job, source, destination)
        plan = self.service.plans.get_by_id(self.job.active_plan_id)
        assert plan is not None

        parent_pl = next(it for it in plan.items if it.entity_type is EntityType.PLAYLIST)
        self.assertEqual(parent_pl.planned_status, ItemStatus.AMBIGUOUS)

        items = self.service.items.list_for_job(self.job.id)
        parent_item = next(it for it in items if it.entity_type is EntityType.PLAYLIST)
        self.assertEqual(parent_item.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(parent_item.last_error, "playlist_sequence_unresolved")

        child_items = [it for it in items if it.entity_type is EntityType.PLAYLIST_ITEM]
        self.assertEqual(len(child_items), 3)
        for child in child_items:
            self.assertIsNone(child.write_position)

        item_b = next(it for it in child_items if it.source_id == "b")
        self.assertEqual(item_b.status, ItemStatus.NOT_FOUND)

    def test_track_ambiguous_blocks_entire_playlist(self) -> None:
        """TRACK -> AMBIGUOUS blocks entire parent playlist sequence."""
        t_a = track("Track A", identifier="a")
        t_b = track("Track B", identifier="b")
        t_c = track("Track C", identifier="c")
        pl = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl1",
            name="Mix",
            tracks=[
                PlaylistItem(position=1, track=t_a),
                PlaylistItem(position=2, track=t_b),
                PlaylistItem(position=3, track=t_c),
            ],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl])
        destination = FakePlatformAdapter(
            display_name="destination",
            tracks=[
                track("Track A", identifier="dst-a"),
                track("Track B", identifier="dst-b1"),
                track("Track B", identifier="dst-b2"),
                track("Track C", identifier="dst-c"),
            ],
        )
        destination.can_reuse_identifier = lambda et, sp: False
        # Mock search to return 2 identical candidates for Track B, making it AMBIGUOUS
        destination.search_track = lambda t, limit=5: (  # type: ignore[method-assign]
            [track("Track B", identifier="dst-b1"), track("Track B", identifier="dst-b2")]
            if t.title == "Track B"
            else [track(t.title, identifier=f"dst-{t.source_id}")]
        )

        self.service.analyze(self.job, source, destination)
        items = self.service.items.list_for_job(self.job.id)
        parent_item = next(it for it in items if it.entity_type is EntityType.PLAYLIST)
        self.assertEqual(parent_item.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(parent_item.last_error, "playlist_sequence_unresolved")

        child_items = [it for it in items if it.entity_type is EntityType.PLAYLIST_ITEM]
        for child in child_items:
            self.assertIsNone(child.write_position)

    def test_mixed_playlist_with_track_not_found_has_zero_write_positions(self) -> None:
        """Mixed playlist with one NOT_FOUND track gives write_position = None to all items."""
        t_a = track("Track A", identifier="a")
        t_b = track("Track B", identifier="b")
        v_v = LibraryRecord(source_platform=Platform.TIDAL, source_id="v", title="Video V")
        pl = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl1",
            name="Mix",
            tracks=[
                PlaylistItem(position=1, track=t_a),
                PlaylistItem(position=2, track=t_b),
                PlaylistItem(position=3, video=v_v),
            ],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl])
        destination = FakePlatformAdapter(
            display_name="destination",
            tracks=[track("Track A", identifier="dst-a")],
            videos=[LibraryRecord(source_platform=Platform.TIDAL, source_id="v", title="Video V")],
        )
        destination.can_reuse_identifier = lambda et, sp: False

        self.service.analyze(self.job, source, destination)
        items = self.service.items.list_for_job(self.job.id)
        child_items = [it for it in items if it.entity_type is EntityType.PLAYLIST_ITEM]
        for child in child_items:
            self.assertIsNone(child.write_position)

    def test_blocked_track_playlist_produces_zero_remote_writes(self) -> None:
        """Blocked playlist produces zero create_playlist and zero add_playlist_item/add_playlist_media calls."""
        t_a = track("Track A", identifier="a")
        t_b = track("Track B", identifier="b")
        v_v = LibraryRecord(source_platform=Platform.TIDAL, source_id="v", title="Video V")
        pl = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl1",
            name="Mix",
            tracks=[
                PlaylistItem(position=1, track=t_a),
                PlaylistItem(position=2, track=t_b),
                PlaylistItem(position=3, video=v_v),
            ],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl])
        destination = FakePlatformAdapter(
            display_name="destination",
            tracks=[track("Track A", identifier="dst-a")],
            videos=[LibraryRecord(source_platform=Platform.TIDAL, source_id="v", title="Video V")],
        )
        destination.can_reuse_identifier = lambda et, sp: False

        self.service.analyze(self.job, source, destination)
        self.service.confirm_plan(
            self.job,
            plan_id=self.job.active_plan_id,
            revision=self.job.active_plan_revision,
            plan_hash=self.job.active_plan_hash,
        )

        create_calls = []
        destination.create_playlist = lambda name: (create_calls.append(name) or "dst-pl1")  # type: ignore[method-assign]
        write_calls = []
        destination.add_playlist_item = lambda pid, tid: write_calls.append((pid, tid))  # type: ignore[method-assign]
        destination.add_playlist_media = lambda pid, ref: write_calls.append((pid, ref))  # type: ignore[method-assign]

        self.service.execute(self.job, destination, confirmed=True)

        self.assertEqual(len(create_calls), 0)
        self.assertEqual(len(write_calls), 0)

    def test_track_resolution_failure_does_not_block_sibling_playlist(self) -> None:
        """One blocked playlist does not block independent sibling playlists."""
        pl_blocked = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl_fail",
            name="Blocked",
            tracks=[
                PlaylistItem(position=1, track=track("Track A", identifier="a")),
                PlaylistItem(position=2, track=track("Missing", identifier="missing")),
            ],
        )
        pl_ok = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl_ok",
            name="Ok",
            tracks=[
                PlaylistItem(position=1, track=track("Track C", identifier="c")),
                PlaylistItem(position=2, track=track("Track D", identifier="d")),
            ],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl_blocked, pl_ok])
        destination = FakePlatformAdapter(
            display_name="destination",
            tracks=[
                track("Track A", identifier="dst-a"),
                track("Track C", identifier="dst-c"),
                track("Track D", identifier="dst-d"),
            ],
        )
        destination.can_reuse_identifier = lambda et, sp: False

        self.service.analyze(self.job, source, destination)
        self.service.confirm_plan(
            self.job,
            plan_id=self.job.active_plan_id,
            revision=self.job.active_plan_revision,
            plan_hash=self.job.active_plan_hash,
        )

        items = self.service.items.list_for_job(self.job.id)
        pl_blocked_item = next(it for it in items if it.source_id == "pl_fail")
        pl_ok_item = next(it for it in items if it.source_id == "pl_ok")

        self.assertEqual(pl_blocked_item.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(pl_ok_item.status, ItemStatus.PENDING)

        pl_ok_children = [
            it for it in items if it.container_source_id == "pl_ok" and it.entity_type is EntityType.PLAYLIST_ITEM
        ]
        self.assertEqual(len(pl_ok_children), 2)
        self.assertEqual(pl_ok_children[0].write_position, 0)
        self.assertEqual(pl_ok_children[1].write_position, 1)

        self.service.execute(self.job, destination, confirmed=True)
        self.assertEqual(destination.playlist_item_ids("dst-pl_ok"), ["dst-c", "dst-d"])
        self.assertNotIn("dst-pl_fail", [p.source_id for p in destination.playlists])


class HeterogeneousExecutionAndRecoveryTests(unittest.TestCase):
    """Execution, reconciliation, and recovery tests for heterogeneous media sequences."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = build_service(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_execute_heterogeneous_sequence(self) -> None:
        """Executing a confirmed plan with TRACK and VIDEO preserves exact typed order."""
        t1 = track("Track 1", identifier="t1")
        v1 = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="v1",
            title="Video 1",
        )
        t2 = track("Track 2", identifier="t2")
        pl = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl1",
            name="Hetero Playlist",
            tracks=[
                PlaylistItem(position=1, track=t1),
                PlaylistItem(position=2, video=v1),
                PlaylistItem(position=3, track=t2),
            ],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl])
        destination = FakePlatformAdapter(display_name="destination")

        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=job.active_plan_id,
            revision=job.active_plan_revision,
            plan_hash=job.active_plan_hash,
        )
        self.service.execute(job, destination, confirmed=True)

        media_order = destination.playlist_media_order("dst-pl1")
        self.assertEqual(
            media_order,
            [
                PlaylistMediaRef(EntityType.TRACK, "t1"),
                PlaylistMediaRef(EntityType.VIDEO, "v1"),
                PlaylistMediaRef(EntityType.TRACK, "t2"),
            ],
        )

    def test_reconciliation_detects_type_mismatch_in_prefix(self) -> None:
        """Reconciliation against destination state marks item ambiguous if types diverge."""
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        container_item = TransferItem.create(
            job.id,
            EntityType.PLAYLIST,
            Platform.TIDAL,
            "pl1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_PLAYLIST,
        )
        container_item.destination_id = "dst-pl1"
        container_item.status = ItemStatus.TRANSFERRED

        # Expected: position 0 is VIDEO v1
        item_0 = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "v1",
            Platform.TIDAL,
            original_position=0,
            write_position=0,
            container_source_id="pl1",
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
            playlist_item_type=EntityType.VIDEO,
        )
        item_0.destination_id = "v1"
        item_0.status = ItemStatus.TRANSFERRED

        # Expected: position 1 is TRACK t2
        item_1 = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "t2",
            Platform.TIDAL,
            original_position=1,
            write_position=1,
            container_source_id="pl1",
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
            playlist_item_type=EntityType.TRACK,
        )
        item_1.destination_id = "t2"
        item_1.status = ItemStatus.MATCHED

        self.service.items.add_many([container_item, item_0, item_1])

        # Destination has TRACK v1 instead of VIDEO v1 at position 0
        destination = FakePlatformAdapter(display_name="destination")
        destination.playlists.append(
            Playlist(
                source_platform=Platform.TIDAL,
                source_id="dst-pl1",
                name="Pl1",
                tracks=[PlaylistItem(position=1, track=track("v1", identifier="v1"))],
            )
        )

        executor = TransferExecutor(
            destination,
            self.service.items,
        )
        with self.assertRaises(AmbiguousOperationError) as ctx:
            executor._write_playlist_item(item_1, [container_item, item_0, item_1])
        self.assertEqual(str(ctx.exception), "playlist_resume_mismatch")
        self.assertEqual(item_1.status, ItemStatus.AMBIGUOUS)

    def test_blocked_playlist_results_in_zero_writes(self) -> None:
        """A blocked playlist produces 0 create_playlist and 0 item write calls."""
        v = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="v1",
            title="Video 1",
        )
        pl = Playlist(
            source_platform=Platform.TIDAL,
            source_id="pl1",
            name="Broken",
            tracks=[PlaylistItem(position=1, video=v)],
        )
        source = FakePlatformAdapter(display_name="source", playlists=[pl])
        destination = FakePlatformAdapter(display_name="destination")
        destination.can_reuse_identifier = lambda et, sp: et != EntityType.VIDEO

        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=job.active_plan_id,
            revision=job.active_plan_revision,
            plan_hash=job.active_plan_hash,
        )
        self.service.execute(job, destination, confirmed=True)

        self.assertEqual(len(destination.playlists), 0)
        self.assertEqual(len(destination.write_calls), 0)

    def test_executor_inconclusive_readback_does_not_blindly_retry(self) -> None:
        """Malformed/inconclusive destination readback marks item AMBIGUOUS and issues 0 remote writes."""
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        destination = FakePlatformAdapter(display_name="destination")
        container_item = TransferItem.create(
            job.id,
            EntityType.PLAYLIST,
            Platform.TIDAL,
            "pl1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_PLAYLIST,
        )
        container_item.destination_id = "dst-pl1"
        container_item.status = ItemStatus.TRANSFERRED

        item_a = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "a",
            Platform.TIDAL,
            original_position=0,
            write_position=0,
            container_source_id="pl1",
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
            mutation_state=MutationState.IN_FLIGHT,
            playlist_item_type=EntityType.TRACK,
        )
        item_a.destination_id = "dst-a"
        item_a.container_destination_id = "dst-pl1"
        item_a.status = ItemStatus.MATCHED
        self.service.items.add_many([container_item, item_a])
        job.status = JobStatus.IMPORTING
        self.service.jobs.update(job)

        # Make destination.playlist_media_order fail (malformed readback)
        destination.playlist_media_order = MagicMock(side_effect=RuntimeError("unknown/malformed occurrence"))  # type: ignore[method-assign]

        remote_writes: list[Any] = []
        destination.add_playlist_item = lambda cid, tid: remote_writes.append((cid, tid))  # type: ignore[method-assign]
        destination.add_playlist_media = lambda cid, ref: remote_writes.append((cid, ref))  # type: ignore[method-assign]

        self.service.resume(job, destination, confirmed=True)

        reconciled = next(it for it in self.service.items.list_for_job(job.id) if it.id == item_a.id)
        self.assertEqual(reconciled.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(reconciled.last_error, "playlist_state_inconclusive")
        self.assertEqual(len(remote_writes), 0, "must issue zero remote writes on inconclusive readback")


class HeterogeneousVerificationTests(unittest.TestCase):
    """Verifier tests for typed media sequences."""

    def test_compare_sequences_exact_typed_success(self) -> None:
        """compare_sequences succeeds when typed media items match exactly."""
        expected = [
            PlaylistMediaRef(EntityType.TRACK, "t1"),
            PlaylistMediaRef(EntityType.VIDEO, "v1"),
        ]
        actual = [
            PlaylistMediaRef(EntityType.TRACK, "t1"),
            PlaylistMediaRef(EntityType.VIDEO, "v1"),
        ]
        comparison = compare_sequences(expected, actual)
        self.assertEqual(comparison.expected_count, 2)
        self.assertEqual(comparison.actual_count, 2)
        self.assertEqual(comparison.missing, [])
        self.assertEqual(comparison.unexpected, [])
        self.assertEqual(comparison.order_mismatches, [])

    def test_compare_sequences_type_mismatch_fails(self) -> None:
        """compare_sequences detects type mismatch between TRACK and VIDEO with same ID."""
        expected = [
            PlaylistMediaRef(EntityType.TRACK, "100"),
        ]
        actual = [
            PlaylistMediaRef(EntityType.VIDEO, "100"),
        ]
        comparison = compare_sequences(expected, actual)
        self.assertEqual(comparison.missing, ["track:100"])
        self.assertEqual(comparison.unexpected, ["video:100"])
        self.assertEqual(len(comparison.order_mismatches), 1)
        self.assertEqual(
            comparison.order_mismatches[0],
            {"position": 0, "expected": "track:100", "actual": "video:100"},
        )

    def test_verifier_verify_playlist_typed(self) -> None:
        """TransferVerifier.verify_playlist validates typed sequences via playlist_media_order."""
        dest = FakePlatformAdapter()
        t = track("Track 1", identifier="t1")
        v = LibraryRecord(source_platform=Platform.TIDAL, source_id="v1", title="Video 1")
        dest.playlists.append(
            Playlist(
                source_platform=Platform.TIDAL,
                source_id="pl-dst",
                name="Pl",
                tracks=[
                    PlaylistItem(position=1, track=t),
                    PlaylistItem(position=2, video=v),
                ],
            )
        )
        verifier = TransferVerifier(dest)
        # Exact typed sequence succeeds
        res_ok = verifier.verify_playlist(
            "pl-dst",
            [
                PlaylistMediaRef(EntityType.TRACK, "t1"),
                PlaylistMediaRef(EntityType.VIDEO, "v1"),
            ],
        )
        self.assertTrue(res_ok.success)

        # Sequence expecting track instead of video fails
        res_fail = verifier.verify_playlist(
            "pl-dst",
            [
                PlaylistMediaRef(EntityType.TRACK, "t1"),
                PlaylistMediaRef(EntityType.TRACK, "v1"),
            ],
        )
        self.assertFalse(res_fail.success)
        self.assertEqual(res_fail.missing, ["track:v1"])
        self.assertEqual(res_fail.unexpected, ["video:v1"])

    def test_verifier_fails_when_transferred_playlist_item_type_missing(self) -> None:
        """Verifier fails closed when a transferred playlist item has playlist_item_type=None."""
        dest = FakePlatformAdapter()
        t = track("Track 1", identifier="t1")
        dest.playlists.append(
            Playlist(
                source_platform=Platform.TIDAL,
                source_id="dst-pl1",
                name="Pl1",
                tracks=[PlaylistItem(position=1, track=t)],
            )
        )
        item = TransferItem.create(
            "job-1",
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "t1",
            Platform.TIDAL,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
            original_position=0,
            write_position=0,
        )
        item.destination_id = "t1"
        item.container_destination_id = "dst-pl1"
        item.status = ItemStatus.TRANSFERRED
        # Force missing playlist_item_type to test fail-closed verifier
        item.playlist_item_type = None

        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
        )
        results = verifier.verify_job(job, [item])
        pl_res = results.get("playlist:dst-pl1")
        self.assertIsNotNone(pl_res)
        assert pl_res is not None
        self.assertFalse(pl_res.success)
        self.assertIn("playlist_item_type_missing", pl_res.warnings)

    def test_verifier_does_not_default_missing_type_to_track(self) -> None:
        """Verifier does not treat missing playlist_item_type as TRACK even if destination has TRACK."""
        dest = FakePlatformAdapter()
        t = track("Track 123", identifier="123")
        dest.playlists.append(
            Playlist(
                source_platform=Platform.TIDAL,
                source_id="dst-pl1",
                name="Pl1",
                tracks=[PlaylistItem(position=1, track=t)],
            )
        )
        item = TransferItem.create(
            "job-1",
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "123",
            Platform.TIDAL,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
            original_position=0,
            write_position=0,
        )
        item.destination_id = "123"
        item.container_destination_id = "dst-pl1"
        item.status = ItemStatus.TRANSFERRED
        item.playlist_item_type = None

        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
        )
        results = verifier.verify_job(job, [item])
        self.assertFalse(results["playlist:dst-pl1"].success)


class TidalStrictReadbackTests(unittest.TestCase):
    """Tests for strict typed readback in TidalLibraryClient and TidalAdapter."""

    def test_tidal_playlist_media_order_rejects_unknown_media_kind(self) -> None:
        """TidalLibraryClient and TidalAdapter reject unknown media kinds during readback."""
        client = TidalLibraryClient(MagicMock(), logging.getLogger("test"))
        fake_playlist = MagicMock()

        class UnknownMedia:
            id = "item-1"

        fake_playlist.items.side_effect = lambda limit=100, offset=0: [UnknownMedia()] if offset == 0 else []
        client._playlist = MagicMock(return_value=fake_playlist)  # type: ignore[method-assign]

        with self.assertRaises(TidalClientError) as ctx:
            client.playlist_media_order("pl-1")
        self.assertEqual(ctx.exception.reason, "playlist_media_type_unsupported")

        # Also test TidalAdapter
        adapter = TidalAdapter(client)
        client.playlist_media_order = MagicMock(return_value=[{"id": "item-1", "kind": "podcast"}])  # type: ignore[method-assign]
        with self.assertRaises(TidalClientError) as ctx_adapter:
            adapter.playlist_media_order("pl-1")
        self.assertEqual(ctx_adapter.exception.reason, "playlist_media_type_unsupported")

    def test_tidal_playlist_media_order_rejects_missing_media_id(self) -> None:
        """TidalLibraryClient and TidalAdapter reject playlist entries without an identifier."""
        client = TidalLibraryClient(MagicMock(), logging.getLogger("test"))
        fake_playlist = MagicMock()

        class TrackWithoutId:
            pass

        fake_playlist.items.side_effect = lambda limit=100, offset=0: [TrackWithoutId()] if offset == 0 else []
        client._playlist = MagicMock(return_value=fake_playlist)  # type: ignore[method-assign]

        with self.assertRaises(TidalClientError) as ctx:
            client.playlist_media_order("pl-1")
        self.assertEqual(ctx.exception.reason, "provider_id_missing")

        # Also test TidalAdapter
        adapter = TidalAdapter(client)
        client.playlist_media_order = MagicMock(return_value=[{"id": "", "kind": "track"}])  # type: ignore[method-assign]
        with self.assertRaises(TidalClientError) as ctx_adapter:
            adapter.playlist_media_order("pl-1")
        self.assertEqual(ctx_adapter.exception.reason, "provider_id_missing")

    def test_unknown_tidal_playlist_object_is_not_classified_as_track(self) -> None:
        """Non-track, non-video objects must not default to track."""
        client = TidalLibraryClient(MagicMock(), logging.getLogger("test"))
        fake_playlist = MagicMock()

        class OtherMedia:
            id = "other-1"

        fake_playlist.items.side_effect = lambda limit=100, offset=0: [OtherMedia()] if offset == 0 else []
        client._playlist = MagicMock(return_value=fake_playlist)  # type: ignore[method-assign]

        with self.assertRaises(TidalClientError) as ctx:
            client.playlist_media_order("pl-1")
        self.assertEqual(ctx.exception.reason, "playlist_media_type_unsupported")


if __name__ == "__main__":
    unittest.main()

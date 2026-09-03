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
    TransferPlan,
    TransferPlanItem,
)
from music_transfer.core.enums import (
    ContentType,
    EntityType,
    ItemStatus,
    MatchMethod,
    Platform,
    TransferOperation,
)
from music_transfer.core.errors import AmbiguousOperationError
from music_transfer.core.transfer.executor import TransferExecutor
from music_transfer.core.transfer.verifier import TransferVerifier, compare_sequences
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
)
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


if __name__ == "__main__":
    unittest.main()

"""Unit tests for playlist sequencing, resume, recovery, and deterministic write positions.

Covers all 15 scenarios specified for Phase 1.2:
1. Gap before successful item (test_resume_after_unmatched_prefix_does_not_duplicate)
2. Gap in the middle
3. Multiple unresolved gaps
4. Duplicate plus gap (test_resume_duplicate_after_unmatched_gap_is_occurrence_aware)
5. Normal duplicate (preserves duplicates)
6. Destination success before checkpoint
7. Timeout after possible destination commit (reconciliation confirms item)
8. Timeout with confirmed absence (re-raises for retry)
9. Timeout with inconclusive state (marks ambiguous, fails safely)
10. Destination sequence divergence (expected prefix mismatch fails safely)
11. Verifier ignores unresolved source gaps
12. Write positions survive serialization (as_dict / from_dict)
13. Legacy item without write_position (backward compatibility)
14. Multiple playlists (independent write position scoping)
15. Invalid duplicate write positions (rejected by validator)
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from music_transfer.app.services import TransferService
from music_transfer.core.domain import Playlist, PlaylistItem, TransferItem
from music_transfer.core.enums import (
    ContentType,
    EntityType,
    ItemStatus,
    JobStatus,
    Platform,
    TransferOperation,
)
from music_transfer.core.errors import (
    TemporaryPlatformError,
    TransferConfigurationError,
    UnsupportedCapabilityError,
)
from music_transfer.core.transfer import TransferVerifier
from music_transfer.core.transfer.planner import (
    migrate_legacy_write_positions,
    validate_plan_write_positions,
)
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
)

from tests.support import FakePlatformAdapter, playlist, track


def build_service(root: Path) -> TransferService:
    return TransferService(
        JsonTransferJobRepository(root), JsonTransferItemRepository(root)
    )


class PlaylistRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = build_service(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # Test 1: Gap before successful item
    # -------------------------------------------------------------------------
    def test_resume_after_unmatched_prefix_does_not_duplicate(self) -> None:
        """Test 1: Gap before successful item.

        Source:
            X -> NOT_FOUND (original_pos 0, write_pos None)
            A -> MATCHED   (original_pos 1, write_pos 0)

        When [A] is already at the destination, resume must produce zero additional A writes.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        # Pre-populate destination with the playlist container and track A
        destination = FakePlatformAdapter(display_name="destination")
        destination.playlists.append(
            Playlist(
                source_platform=Platform.TIDAL,
                source_id="dst-pl1",
                name="Pl1",
                tracks=[PlaylistItem(position=1, track=track("A", identifier="dst-a"))],
            )
        )

        # Plan items: playlist container already transferred; X not found; A matched at write_pos 0
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

        item_x = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "x",
            Platform.TIDAL,
            container_source_id="pl1",
            original_position=0,
            write_position=None,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_x.mark(ItemStatus.NOT_FOUND)

        item_a = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "a",
            Platform.TIDAL,
            container_source_id="pl1",
            original_position=1,
            write_position=0,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_a.destination_id = "dst-a"
        item_a.status = ItemStatus.MATCHED  # Simulating checkpoint loss: remote has it, local is MATCHED

        self.service.items.add_many([container_item, item_x, item_a])

        # Mark job as IMPORTING (interrupted state)
        job.status = JobStatus.IMPORTING
        self.service.jobs.update(job)

        writes_before = len(destination.write_calls)
        self.service.resume(job, destination, confirmed=True)

        new_writes = [
            c for c in destination.write_calls[writes_before:]
            if c[0] == "add_playlist_item"
        ]
        self.assertEqual(len(new_writes), 0)
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["dst-a"])
        reloaded_a = next(it for it in self.service.items.list_for_job(job.id) if it.source_id == "a")
        self.assertEqual(reloaded_a.status, ItemStatus.TRANSFERRED)

    # -------------------------------------------------------------------------
    # Test 2: Gap in the middle
    # -------------------------------------------------------------------------
    def test_gap_in_the_middle_write_positions_and_resume(self) -> None:
        """Test 2: Gap in the middle.

        Source:
            A -> MATCHED   (original 0, write 0)
            X -> NOT_FOUND (original 1, write None)
            B -> MATCHED   (original 2, write 1)

        Destination:
            [A, B]

        Resume must make zero duplicate writes.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        destination = FakePlatformAdapter(display_name="destination")
        destination.playlists.append(
            Playlist(
                source_platform=Platform.TIDAL,
                source_id="dst-pl1",
                name="Pl1",
                tracks=[
                    PlaylistItem(position=1, track=track("A", identifier="dst-a")),
                    PlaylistItem(position=2, track=track("B", identifier="dst-b")),
                ],
            )
        )

        container_item = TransferItem.create(
            job.id, EntityType.PLAYLIST, Platform.TIDAL, "pl1", Platform.TIDAL,
            operation=TransferOperation.CREATE_PLAYLIST,
        )
        container_item.destination_id = "dst-pl1"
        container_item.status = ItemStatus.TRANSFERRED

        item_a = TransferItem.create(
            job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "a", Platform.TIDAL,
            container_source_id="pl1", original_position=0, write_position=0,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_a.destination_id = "dst-a"
        item_a.status = ItemStatus.TRANSFERRED

        item_x = TransferItem.create(
            job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "x", Platform.TIDAL,
            container_source_id="pl1", original_position=1, write_position=None,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_x.mark(ItemStatus.NOT_FOUND)

        item_b = TransferItem.create(
            job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "b", Platform.TIDAL,
            container_source_id="pl1", original_position=2, write_position=1,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_b.destination_id = "dst-b"
        item_b.status = ItemStatus.MATCHED  # Destination has it, local crashed before marking TRANSFERRED

        self.service.items.add_many([container_item, item_a, item_x, item_b])

        job.status = JobStatus.IMPORTING
        self.service.jobs.update(job)

        writes_before = len(destination.write_calls)
        self.service.resume(job, destination, confirmed=True)

        new_writes = [
            c for c in destination.write_calls[writes_before:]
            if c[0] == "add_playlist_item"
        ]
        self.assertEqual(len(new_writes), 0)
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["dst-a", "dst-b"])
        reloaded_b = next(it for it in self.service.items.list_for_job(job.id) if it.source_id == "b")
        self.assertEqual(reloaded_b.status, ItemStatus.TRANSFERRED)

    # -------------------------------------------------------------------------
    # Test 3: Multiple unresolved gaps
    # -------------------------------------------------------------------------
    def test_multiple_unresolved_gaps_assigns_contiguous_write_positions(self) -> None:
        """Test 3: Multiple unresolved gaps.

        Source:
            X -> NOT_FOUND   -> None
            A -> MATCHED     -> 0
            Y -> AMBIGUOUS   -> None
            B -> MATCHED     -> 1
            Z -> UNAVAILABLE -> None
            C -> MATCHED     -> 2
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        items = [
            TransferItem.create(
                job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "x", Platform.TIDAL,
                container_source_id="pl1", original_position=0,
                operation=TransferOperation.ADD_PLAYLIST_ITEM,
            ),
            TransferItem.create(
                job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "a", Platform.TIDAL,
                container_source_id="pl1", original_position=1,
                operation=TransferOperation.ADD_PLAYLIST_ITEM,
            ),
            TransferItem.create(
                job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "y", Platform.TIDAL,
                container_source_id="pl1", original_position=2,
                operation=TransferOperation.ADD_PLAYLIST_ITEM,
            ),
            TransferItem.create(
                job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "b", Platform.TIDAL,
                container_source_id="pl1", original_position=3,
                operation=TransferOperation.ADD_PLAYLIST_ITEM,
            ),
            TransferItem.create(
                job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "z", Platform.TIDAL,
                container_source_id="pl1", original_position=4,
                operation=TransferOperation.ADD_PLAYLIST_ITEM,
            ),
            TransferItem.create(
                job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "c", Platform.TIDAL,
                container_source_id="pl1", original_position=5,
                operation=TransferOperation.ADD_PLAYLIST_ITEM,
            ),
        ]
        items[0].mark(ItemStatus.NOT_FOUND)
        items[1].destination_id = "dst-a"
        items[1].status = ItemStatus.MATCHED
        items[2].mark(ItemStatus.AMBIGUOUS)
        items[3].destination_id = "dst-b"
        items[3].status = ItemStatus.MATCHED
        items[4].mark(ItemStatus.UNAVAILABLE)
        items[5].destination_id = "dst-c"
        items[5].status = ItemStatus.MATCHED

        migrate_legacy_write_positions(items)
        validate_plan_write_positions(items)

        by_id = {it.source_id: it for it in items}
        self.assertIsNone(by_id["x"].write_position)
        self.assertEqual(by_id["a"].write_position, 0)
        self.assertIsNone(by_id["y"].write_position)
        self.assertEqual(by_id["b"].write_position, 1)
        self.assertIsNone(by_id["z"].write_position)
        self.assertEqual(by_id["c"].write_position, 2)

    # -------------------------------------------------------------------------
    # Test 4: Duplicate plus gap (occurrence aware)
    # -------------------------------------------------------------------------
    def test_resume_duplicate_after_unmatched_gap_is_occurrence_aware(self) -> None:
        """Test 4: Duplicate plus gap.

        Source:
            A -> MATCHED (original 0, write 0)
            X -> NOT_FOUND (original 1, write None)
            A -> MATCHED (original 2, write 1)

        Destination:
            [A, A]

        Resume must recognize both occurrences and not append a third A.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        destination = FakePlatformAdapter(display_name="destination")
        destination.playlists.append(
            Playlist(
                source_platform=Platform.TIDAL,
                source_id="dst-pl1",
                name="Pl1",
                tracks=[
                    PlaylistItem(position=1, track=track("A", identifier="dst-a")),
                    PlaylistItem(position=2, track=track("A", identifier="dst-a")),
                ],
            )
        )

        container_item = TransferItem.create(
            job.id, EntityType.PLAYLIST, Platform.TIDAL, "pl1", Platform.TIDAL,
            operation=TransferOperation.CREATE_PLAYLIST,
        )
        container_item.destination_id = "dst-pl1"
        container_item.status = ItemStatus.TRANSFERRED

        item_a1 = TransferItem.create(
            job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "a1", Platform.TIDAL,
            container_source_id="pl1", original_position=0, write_position=0,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_a1.destination_id = "dst-a"
        item_a1.status = ItemStatus.TRANSFERRED

        item_x = TransferItem.create(
            job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "x", Platform.TIDAL,
            container_source_id="pl1", original_position=1, write_position=None,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_x.mark(ItemStatus.NOT_FOUND)

        item_a2 = TransferItem.create(
            job.id, EntityType.PLAYLIST_ITEM, Platform.TIDAL, "a2", Platform.TIDAL,
            container_source_id="pl1", original_position=2, write_position=1,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_a2.destination_id = "dst-a"
        item_a2.status = ItemStatus.MATCHED  # Destination has second A, local crashed before marking TRANSFERRED

        self.service.items.add_many([container_item, item_a1, item_x, item_a2])

        job.status = JobStatus.IMPORTING
        self.service.jobs.update(job)

        writes_before = len(destination.write_calls)
        self.service.resume(job, destination, confirmed=True)

        new_writes = [
            c for c in destination.write_calls[writes_before:]
            if c[0] == "add_playlist_item"
        ]
        self.assertEqual(len(new_writes), 0)
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["dst-a", "dst-a"])
        reloaded_a2 = next(it for it in self.service.items.list_for_job(job.id) if it.source_id == "a2")
        self.assertEqual(reloaded_a2.status, ItemStatus.TRANSFERRED)

    # -------------------------------------------------------------------------
    # Test 5: Normal duplicate
    # -------------------------------------------------------------------------
    def test_normal_duplicate_preserves_duplicate_sequence(self) -> None:
        """Test 5: Normal duplicate.

        Source:
            A, B, A
        Destination:
            [A, B, A]
        Ensure no deduplication regression.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        tracks = [
            track("A", identifier="a1"),
            track("B", identifier="b"),
            track("A", identifier="a2"),
        ]
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", tracks)]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        self.service.execute(job, destination, confirmed=True)
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["a1", "b", "a2"])

    # -------------------------------------------------------------------------
    # Test 6: Destination success before checkpoint
    # -------------------------------------------------------------------------
    def test_destination_success_before_checkpoint_recovers_without_duplicate(self) -> None:
        """Test 6: Destination success before checkpoint.

        Simulate:
            destination.add_playlist_item(A) -> success
            repository update/checkpoint -> crash/exception
        Resume:
            destination contains A at expected write position
            no second mutation
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", [track("A", identifier="a")])]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        # Simulate destination already having received the item before checkpoint was persisted
        destination.playlists.append(
            Playlist(
                source_platform=Platform.TIDAL,
                source_id="dst-pl1",
                name="Pl1",
                tracks=[PlaylistItem(position=1, track=track("A", identifier="a"))],
            )
        )

        items = self.service.items.list_for_job(job.id)
        container_item = next(it for it in items if it.entity_type is EntityType.PLAYLIST)
        container_item.destination_id = "dst-pl1"
        container_item.status = ItemStatus.TRANSFERRED
        self.service.items.update(container_item)

        track_item = next(it for it in items if it.entity_type is EntityType.PLAYLIST_ITEM)
        track_item.container_destination_id = "dst-pl1"
        track_item.destination_id = "a"
        track_item.write_position = 0
        track_item.status = ItemStatus.MATCHED  # Uncheckpointed!
        self.service.items.update(track_item)

        job.status = JobStatus.IMPORTING
        self.service.jobs.update(job)

        writes_before = len(destination.write_calls)
        self.service.resume(job, destination, confirmed=True)

        new_writes = [
            c for c in destination.write_calls[writes_before:]
            if c[0] == "add_playlist_item"
        ]
        self.assertEqual(len(new_writes), 0)
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["a"])
        reloaded = next(it for it in self.service.items.list_for_job(job.id) if it.source_id == "a")
        self.assertEqual(reloaded.status, ItemStatus.TRANSFERRED)

    # -------------------------------------------------------------------------
    # Test 7: Timeout after possible destination commit
    # -------------------------------------------------------------------------
    def test_timeout_after_possible_destination_commit_reconciles_as_transferred(self) -> None:
        """Test 7: Timeout after possible destination commit.

        Mutation raises TemporaryPlatformError (e.g. timeout), but remote state
        contains the committed item.
        Expected: reconciliation confirms item as TRANSFERRED, no retry.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", [track("A", identifier="a")])]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        orig_add = destination.add_playlist_item

        def timeout_after_commit(p_id: str, t_id: str) -> None:
            orig_add(p_id, t_id)
            raise TemporaryPlatformError("network_timeout_after_commit")

        destination.add_playlist_item = timeout_after_commit  # type: ignore[method-assign]

        result = self.service.execute(job, destination, confirmed=True)
        outcome = result["outcome"]
        self.assertEqual(outcome.failed, 0)
        self.assertEqual(outcome.succeeded, 2)  # Container + Track

        # Destination has exactly 1 track
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["a"])
        track_item = next(it for it in self.service.items.list_for_job(job.id) if it.entity_type is EntityType.PLAYLIST_ITEM)
        self.assertEqual(track_item.status, ItemStatus.TRANSFERRED)

    # -------------------------------------------------------------------------
    # Test 8: Timeout with confirmed absence
    # -------------------------------------------------------------------------
    def test_timeout_with_confirmed_absence_propagates_error_for_retry(self) -> None:
        """Test 8: Timeout with confirmed absence.

        Destination state clearly proves the occurrence was not added (len == write_pos).
        Error is re-raised and handled by standard executor retry/failure policy.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", [track("A", identifier="a")])]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        def timeout_without_commit(p_id: str, t_id: str) -> None:
            # Does not add item, raises timeout directly
            raise TemporaryPlatformError("connection_timeout_before_commit")

        destination.add_playlist_item = timeout_without_commit  # type: ignore[method-assign]

        result = self.service.execute(job, destination, confirmed=True)
        outcome = result["outcome"]
        self.assertEqual(outcome.failed, 1)

        self.assertEqual(destination.playlist_item_ids("dst-pl1"), [])
        track_item = next(it for it in self.service.items.list_for_job(job.id) if it.entity_type is EntityType.PLAYLIST_ITEM)
        self.assertEqual(track_item.status, ItemStatus.FAILED)
        self.assertEqual(track_item.last_failure_kind, "temporary")

    # -------------------------------------------------------------------------
    # Test 9: Timeout with inconclusive state
    # -------------------------------------------------------------------------
    def test_timeout_with_inconclusive_state_fails_safely_as_ambiguous(self) -> None:
        """Test 9: Timeout with inconclusive state.

        If destination state cannot prove success or failure (e.g. playlist_item_ids fails),
        fails safely as AmbiguousOperationError without blind duplicate write.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", [track("A", identifier="a")])]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        query_calls = {"count": 0}

        def timeout_write(p_id: str, t_id: str) -> None:
            raise TemporaryPlatformError("write_timeout")

        orig_query = destination.playlist_item_ids

        def broken_query_after_write(p_id: str) -> list[str]:
            query_calls["count"] += 1
            if query_calls["count"] > 1:
                raise RuntimeError("cannot_read_destination_state")
            return orig_query(p_id)

        destination.add_playlist_item = timeout_write  # type: ignore[method-assign]
        destination.playlist_item_ids = broken_query_after_write  # type: ignore[method-assign]

        result = self.service.execute(job, destination, confirmed=True)
        outcome = result["outcome"]
        self.assertEqual(outcome.failed, 1)

        track_item = next(it for it in self.service.items.list_for_job(job.id) if it.entity_type is EntityType.PLAYLIST_ITEM)
        self.assertEqual(track_item.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(track_item.last_failure_kind, "ambiguous")

    # -------------------------------------------------------------------------
    # Test 10: Destination sequence divergence
    # -------------------------------------------------------------------------
    def test_destination_sequence_divergence_fails_safely(self) -> None:
        """Test 10: Destination sequence divergence.

        Expected: [A, B]
        Actual: [A, X]
        Recovery must not blindly append B as if actual state were a valid prefix.
        Must mark ambiguous and halt safely.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        tracks = [track("A", identifier="a"), track("B", identifier="b")]
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", tracks)]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        # Pre-populate destination with diverged content: [A, Foreign]
        destination.playlists.append(
            Playlist(
                source_platform=Platform.TIDAL,
                source_id="dst-pl1",
                name="Pl1",
                tracks=[
                    PlaylistItem(position=1, track=track("A", identifier="a")),
                    PlaylistItem(position=2, track=track("Foreign", identifier="x")),
                ],
            )
        )

        items = self.service.items.list_for_job(job.id)
        container_item = next(it for it in items if it.entity_type is EntityType.PLAYLIST)
        container_item.destination_id = "dst-pl1"
        container_item.status = ItemStatus.TRANSFERRED
        self.service.items.update(container_item)

        item_a = next(it for it in items if it.source_id == "a")
        item_a.destination_id = "a"
        item_a.container_destination_id = "dst-pl1"
        item_a.write_position = 0
        item_a.status = ItemStatus.TRANSFERRED
        self.service.items.update(item_a)

        item_b = next(it for it in items if it.source_id == "b")
        item_b.destination_id = "b"
        item_b.container_destination_id = "dst-pl1"
        item_b.write_position = 1
        item_b.status = ItemStatus.MATCHED
        self.service.items.update(item_b)

        # Executing should hit reconciliation for B and fail safely due to [a, x] != [a, b][:2]
        self.service.execute(job, destination, confirmed=True)

        item_b_reloaded = next(it for it in self.service.items.list_for_job(job.id) if it.source_id == "b")
        self.assertEqual(item_b_reloaded.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(item_b_reloaded.last_failure_kind, "ambiguous")
        self.assertEqual(item_b_reloaded.last_error, "playlist_resume_mismatch")
        # Ensure B was NOT appended to destination
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["a", "x"])

    # -------------------------------------------------------------------------
    # Test 11: Verifier ignores unresolved source gaps
    # -------------------------------------------------------------------------
    def test_verifier_ignores_unresolved_source_gaps(self) -> None:
        """Test 11: Verifier ignores unresolved source gaps.

        Source:
            A
            X -> NOT_FOUND
            B
        Verification expects:
            [A, B]
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        tracks = [
            track("A", identifier="a"),
            track("X", identifier="x"),
            track("B", identifier="b"),
        ]
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", tracks)]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        items = self.service.items.list_for_job(job.id)
        for item in items:
            if item.source_id == "x":
                item.mark(ItemStatus.NOT_FOUND)
                item.write_position = None
            elif item.source_id == "a":
                item.destination_id = "dst-a"
                item.write_position = 0
            elif item.source_id == "b":
                item.destination_id = "dst-b"
                item.write_position = 1
            self.service.items.update(item)

        self.service.execute(job, destination, confirmed=True)
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["dst-a", "dst-b"])

        # Run verifier directly
        verifier = TransferVerifier(destination)
        completed_items = self.service.items.list_for_job(job.id)
        results = verifier.verify_job(job, completed_items)
        pl_result = results["playlist:dst-pl1"]
        self.assertTrue(pl_result.success)
        self.assertEqual(pl_result.expected_count, 2)
        self.assertEqual(pl_result.actual_count, 2)

    # -------------------------------------------------------------------------
    # Test 12: Write positions survive serialization
    # -------------------------------------------------------------------------
    def test_write_positions_survive_serialization(self) -> None:
        """Test 12: Write positions survive serialization.

        operation=ADD_PLAYLIST_ITEM
        original_position=5
        write_position=2
        """
        item = TransferItem.create(
            job_id="job-1",
            entity_type=EntityType.PLAYLIST_ITEM,
            source_platform=Platform.TIDAL,
            source_id="src-track-1",
            destination_platform=Platform.TIDAL,
            original_position=5,
            write_position=2,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        serialized = item.as_dict()
        self.assertEqual(serialized["original_position"], 5)
        self.assertEqual(serialized["write_position"], 2)
        self.assertEqual(serialized["operation"], "add_playlist_item")

        deserialized = TransferItem.from_dict(serialized)
        self.assertEqual(deserialized.original_position, 5)
        self.assertEqual(deserialized.write_position, 2)
        self.assertEqual(deserialized.operation, TransferOperation.ADD_PLAYLIST_ITEM)

    # -------------------------------------------------------------------------
    # Test 13: Legacy item without write_position
    # -------------------------------------------------------------------------
    def test_legacy_item_without_write_position_loads_safely(self) -> None:
        """Test 13: Legacy item without write_position loads safely.

        Loads previous state format without crashing, defaults to None,
        and does not equate original_position to write_position.
        """
        legacy_dict: dict[str, Any] = {
            "id": "item-legacy-1",
            "job_id": "job-legacy",
            "entity_type": "playlist_item",
            "source_platform": "tidal",
            "source_id": "track-legacy",
            "destination_platform": "tidal",
            "original_position": 7,
            "status": "matched",
            "operation": "add_playlist_item",
        }
        loaded = TransferItem.from_dict(legacy_dict)
        self.assertEqual(loaded.original_position, 7)
        self.assertIsNone(loaded.write_position)

    # -------------------------------------------------------------------------
    # Test 14: Multiple playlists
    # -------------------------------------------------------------------------
    def test_multiple_playlists_scope_write_positions_independently(self) -> None:
        """Test 14: Multiple playlists.

        Write positions are scoped independently per destination playlist (0..N-1 each).
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        pl1 = playlist("Pl1", [track("A1", identifier="a1"), track("A2", identifier="a2")])
        pl2 = playlist("Pl2", [track("B1", identifier="b1"), track("B2", identifier="b2")])

        source = FakePlatformAdapter(display_name="source", playlists=[pl1, pl2])
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        items = self.service.items.list_for_job(job.id)
        pl1_items = [
            it for it in items
            if it.entity_type is EntityType.PLAYLIST_ITEM and it.container_source_id == "pl1"
        ]
        pl2_items = [
            it for it in items
            if it.entity_type is EntityType.PLAYLIST_ITEM and it.container_source_id == "pl2"
        ]

        self.assertEqual([it.write_position for it in pl1_items], [0, 1])
        self.assertEqual([it.write_position for it in pl2_items], [0, 1])

        self.service.execute(job, destination, confirmed=True)
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["a1", "a2"])
        self.assertEqual(destination.playlist_item_ids("dst-pl2"), ["b1", "b2"])

    # -------------------------------------------------------------------------
    # Test 15: Invalid duplicate write positions
    # -------------------------------------------------------------------------
    def test_invalid_duplicate_write_positions_rejected_before_mutation(self) -> None:
        """Test 15: Invalid duplicate write positions.

        Plan:
            A -> 0
            B -> 0
        must be rejected before unsafe mutation.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)
        destination = FakePlatformAdapter(display_name="destination")

        item_a = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "a",
            Platform.TIDAL,
            container_source_id="pl1",
            original_position=0,
            write_position=0,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_a.destination_id = "dst-a"
        item_a.status = ItemStatus.MATCHED

        item_b = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "b",
            Platform.TIDAL,
            container_source_id="pl1",
            original_position=1,
            write_position=0,  # Invalid duplicate!
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_b.destination_id = "dst-b"
        item_b.status = ItemStatus.MATCHED

        self.service.items.add_many([item_a, item_b])

        with self.assertRaises(TransferConfigurationError) as ctx:
            self.service.execute(job, destination, confirmed=True)

        self.assertIn("duplicate_position", str(ctx.exception))
        # Ensure destination was not touched
        self.assertEqual(len(destination.write_calls), 0)

    # -------------------------------------------------------------------------
    # Test 16: Later playlist item is not written when previous planned slot failed
    # -------------------------------------------------------------------------
    def test_later_playlist_item_is_not_written_when_previous_planned_slot_failed(self) -> None:
        """Test 16: Later playlist item is not written when previous planned slot failed.

        Plan:
            A -> 0
            B -> 1
            C -> 2
        Execution:
            A succeeds
            B fails
            C reached -> predecessor missing -> C not written, len(actual) remains 1
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        tracks = [
            track("A", identifier="a"),
            track("B", identifier="b"),
            track("C", identifier="c"),
        ]
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", tracks)]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        # Make B fail when attempting to write
        orig_add = destination.add_playlist_item

        def fail_on_b(playlist_id: str, track_id: str) -> None:
            if track_id == "b":
                destination._record("add_playlist_item", playlist_id, track_id)
                raise TemporaryPlatformError("write_failed_for_b")
            orig_add(playlist_id, track_id)

        destination.add_playlist_item = fail_on_b  # type: ignore[method-assign]

        self.service.execute(job, destination, confirmed=True)

        # Destination must only contain A
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["a"])

        items = self.service.items.list_for_job(job.id)
        item_a = next(it for it in items if it.source_id == "a")
        item_b = next(it for it in items if it.source_id == "b")
        item_c = next(it for it in items if it.source_id == "c")

        self.assertEqual(item_a.status, ItemStatus.TRANSFERRED)
        self.assertEqual(item_a.write_position, 0)

        self.assertEqual(item_b.status, ItemStatus.FAILED)
        self.assertEqual(item_b.write_position, 1)

        # C must not be executed or appended
        self.assertEqual(item_c.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(item_c.write_position, 2)
        self.assertIn(item_c.last_error, ("playlist_predecessor_missing", "playlist_sequence_blocked"))

        # Explicitly verify add_playlist_item mutation calls: A was called, B was called (and failed), C was never called
        mutation_calls = [args for name, args in destination.write_calls if name == "add_playlist_item"]
        self.assertIn(("dst-pl1", "a"), mutation_calls)
        self.assertIn(("dst-pl1", "b"), mutation_calls)
        self.assertNotIn(("dst-pl1", "c"), mutation_calls)

    # -------------------------------------------------------------------------
    # Test 17: Timeout with equal length but invalid prefix is not confirmed absent
    # -------------------------------------------------------------------------
    def test_timeout_equal_length_with_invalid_prefix_is_not_confirmed_absent(self) -> None:
        """Test 17: Timeout with equal length but invalid prefix is not confirmed absent.

        Expected: [A, B]
        B.write_position = 1
        Timeout occurs during B's write.
        Actual: [X] (len is 1, matching B's write_position, but actual != expected[:1]).
        Must NOT be classified as confirmed absent or retried.
        Must become AMBIGUOUS with playlist_resume_mismatch.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        tracks = [track("A", identifier="a"), track("B", identifier="b")]
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", tracks)]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        # Container and item A are transferred
        items = self.service.items.list_for_job(job.id)
        container_item = next(it for it in items if it.entity_type is EntityType.PLAYLIST)
        container_item.destination_id = "dst-pl1"
        container_item.status = ItemStatus.TRANSFERRED
        self.service.items.update(container_item)

        item_a = next(it for it in items if it.source_id == "a")
        item_a.status = ItemStatus.TRANSFERRED
        item_a.destination_id = "a"
        item_a.container_destination_id = "dst-pl1"
        item_a.write_position = 0
        self.service.items.update(item_a)

        item_b = next(it for it in items if it.source_id == "b")
        item_b.status = ItemStatus.MATCHED
        item_b.destination_id = "b"
        item_b.container_destination_id = "dst-pl1"
        item_b.write_position = 1
        self.service.items.update(item_b)

        # Destination starts with [a]
        destination.playlists.append(
            Playlist(
                source_platform=Platform.TIDAL,
                source_id="dst-pl1",
                name="Pl1",
                tracks=[PlaylistItem(position=1, track=track("A", identifier="a"))],
            )
        )

        b_attempts = 0

        def timeout_on_b(playlist_id: str, track_id: str) -> None:
            nonlocal b_attempts
            b_attempts += 1
            # Corrupt destination to [x] to simulate diverged remote state upon timeout
            for p in destination.playlists:
                if p.source_id == playlist_id:
                    p.tracks = [PlaylistItem(position=1, track=track("X", identifier="x"))]
            raise TemporaryPlatformError("network_timeout_on_b")

        destination.add_playlist_item = timeout_on_b  # type: ignore[method-assign]

        self.service.execute(job, destination, confirmed=True)

        item_b_reloaded = next(it for it in self.service.items.list_for_job(job.id) if it.source_id == "b")
        self.assertEqual(item_b_reloaded.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(item_b_reloaded.last_error, "playlist_resume_mismatch")
        self.assertEqual(item_b_reloaded.last_failure_kind, "ambiguous")
        # Ensure B was attempted at most once (never retried blindly)
        self.assertEqual(b_attempts, 1)
        # Destination unchanged
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["x"])

    # -------------------------------------------------------------------------
    # Test 18: Unverifiable playlist commit is not retried blindly
    # -------------------------------------------------------------------------
    def test_unverifiable_playlist_commit_is_not_retried_blindly(self) -> None:
        """Test 18: Unverifiable playlist commit is not retried blindly.

        Destination raises UnsupportedCapabilityError on playlist_item_ids.
        When add_playlist_item raises TemporaryPlatformError or AmbiguousOperationError,
        executor must NOT blindly retry, but mark item as AMBIGUOUS with
        playlist_commit_unverifiable and stop. Total attempts must remain 1.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        tracks = [track("A", identifier="a")]
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", tracks)]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        mutation_calls = 0

        def timeout_mutation(playlist_id: str, track_id: str) -> None:
            nonlocal mutation_calls
            mutation_calls += 1
            raise TemporaryPlatformError("timeout_after_possible_commit")

        def no_inspection(playlist_id: str) -> list[str]:
            raise UnsupportedCapabilityError("playlist_inspection_not_supported")

        destination.add_playlist_item = timeout_mutation  # type: ignore[method-assign]
        destination.playlist_item_ids = no_inspection  # type: ignore[method-assign]

        self.service.execute(job, destination, confirmed=True)

        items = self.service.items.list_for_job(job.id)
        item_a = next(it for it in items if it.source_id == "a")

        self.assertEqual(item_a.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(item_a.last_error, "playlist_commit_unverifiable")
        self.assertEqual(item_a.last_failure_kind, "ambiguous")
        # Must have been attempted exactly once
        self.assertEqual(mutation_calls, 1)

    # -------------------------------------------------------------------------
    # Test 19: Playlist retry job requires replanning before execution
    # -------------------------------------------------------------------------
    def test_playlist_retry_job_requires_replanning_before_execution(self) -> None:
        """Test 19: Playlist retry job requires replanning before execution.

        Original:
            A=0 -> transferred
            B=1 -> failed
            C=2 -> failed
        Retry job clones B and C with write_position=None.
        Executing retry job directly without re-planning must fail before destination mutation.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        tracks = [
            track("A", identifier="a"),
            track("B", identifier="b"),
            track("C", identifier="c"),
        ]
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", tracks)]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        # Simulate execution where A succeeded, B and C failed
        items = self.service.items.list_for_job(job.id)
        container_item = next(it for it in items if it.entity_type is EntityType.PLAYLIST)
        container_item.destination_id = "dst-pl1"
        container_item.status = ItemStatus.TRANSFERRED
        self.service.items.update(container_item)

        item_a = next(it for it in items if it.source_id == "a")
        item_a.status = ItemStatus.TRANSFERRED
        self.service.items.update(item_a)

        item_b = next(it for it in items if it.source_id == "b")
        item_b.status = ItemStatus.FAILED
        self.service.items.update(item_b)

        item_c = next(it for it in items if it.source_id == "c")
        item_c.status = ItemStatus.FAILED
        self.service.items.update(item_c)

        job.status = JobStatus.FAILED
        self.service.jobs.update(job)

        # Create retry job
        retry_job = self.service.create_retry_job(job)
        assert retry_job is not None
        retry_items = self.service.items.list_for_job(retry_job.id)
        self.assertEqual(len(retry_items), 2)
        # Verify write_position was cleared for cloned playlist items
        for r_item in retry_items:
            self.assertIsNone(r_item.write_position)

        # Executing retry job directly without re-planning must fail validation
        retry_job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(retry_job)
        retry_destination = FakePlatformAdapter(display_name="retry_dest")
        with self.assertRaises(TransferConfigurationError) as ctx:
            self.service.execute(retry_job, retry_destination, confirmed=True)

        self.assertIn("missing_write_position", str(ctx.exception))
        # Zero writes attempted on destination
        self.assertEqual(len(retry_destination.write_calls), 0)

    # -------------------------------------------------------------------------
    # Test 20: Retry cloning does not silently renumber playlist positions
    # -------------------------------------------------------------------------
    def test_retry_cloning_does_not_silently_renumber_playlist_positions(self) -> None:
        """Test 20: Retry cloning does not silently renumber playlist positions.

        Ensures create_retry_job preserves original_position, does not invent
        renumbered write positions [0, 1] for [B(original=1), C(original=2)],
        and sets write_position to None.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        tracks = [
            track("A", identifier="a"),
            track("B", identifier="b"),
            track("C", identifier="c"),
        ]
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", tracks)]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        items = self.service.items.list_for_job(job.id)
        item_b = next(it for it in items if it.source_id == "b")
        item_b.status = ItemStatus.FAILED
        self.service.items.update(item_b)

        item_c = next(it for it in items if it.source_id == "c")
        item_c.status = ItemStatus.FAILED
        self.service.items.update(item_c)

        job.status = JobStatus.FAILED
        self.service.jobs.update(job)

        retry_job = self.service.create_retry_job(job)
        assert retry_job is not None
        retry_items = self.service.items.list_for_job(retry_job.id)

        retry_b = next(it for it in retry_items if it.source_id == "b")
        retry_c = next(it for it in retry_items if it.source_id == "c")

        # original_position preserved
        self.assertEqual(retry_b.original_position, 1)
        self.assertEqual(retry_c.original_position, 2)

        # write_position not silently renumbered to 0 and 1
        self.assertIsNone(retry_b.write_position)
        self.assertIsNone(retry_c.write_position)

    # -------------------------------------------------------------------------
    # Test 21: Playlist sequence failure blocks later items in same playlist
    # -------------------------------------------------------------------------
    def test_playlist_sequence_failure_blocks_later_items_in_same_playlist(self) -> None:
        """Test 21: Playlist sequence failure blocks later items in same playlist.

        When an item in a playlist encounters a failure, subsequent items in the
        same playlist are blocked from execution (zero write calls) and marked
        AMBIGUOUS with playlist_sequence_blocked.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        tracks = [
            track("A", identifier="a"),
            track("B", identifier="b"),
            track("C", identifier="c"),
            track("D", identifier="d"),
        ]
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", tracks)]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        orig_add = destination.add_playlist_item

        def fail_on_b(playlist_id: str, track_id: str) -> None:
            if track_id == "b":
                destination._record("add_playlist_item", playlist_id, track_id)
                raise TemporaryPlatformError("failed_b")
            orig_add(playlist_id, track_id)

        destination.add_playlist_item = fail_on_b  # type: ignore[method-assign]

        self.service.execute(job, destination, confirmed=True)

        items = self.service.items.list_for_job(job.id)
        item_a = next(it for it in items if it.source_id == "a")
        item_b = next(it for it in items if it.source_id == "b")
        item_c = next(it for it in items if it.source_id == "c")
        item_d = next(it for it in items if it.source_id == "d")

        self.assertEqual(item_a.status, ItemStatus.TRANSFERRED)
        self.assertEqual(item_b.status, ItemStatus.FAILED)
        self.assertEqual(item_c.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(item_c.last_error, "playlist_sequence_blocked")
        self.assertEqual(item_d.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(item_d.last_error, "playlist_sequence_blocked")

        # Only A was written
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), ["a"])
        # add_playlist_item called only for A and B
        add_calls = [args[1] for name, args in destination.write_calls if name == "add_playlist_item"]
        self.assertEqual(add_calls, ["a", "b"])

    # -------------------------------------------------------------------------
    # Test 22: Playlist sequence failure does not block unrelated playlist
    # -------------------------------------------------------------------------
    def test_playlist_sequence_failure_does_not_block_unrelated_playlist(self) -> None:
        """Test 22: Playlist sequence failure does not block unrelated playlist.

        Failure in Playlist 1 blocks later items in Playlist 1, but items in
        Playlist 2 continue executing and succeed.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        pl1_tracks = [track("A1", identifier="a1"), track("A2", identifier="a2")]
        pl2_tracks = [track("B1", identifier="b1"), track("B2", identifier="b2")]

        source = FakePlatformAdapter(
            display_name="source",
            playlists=[playlist("Pl1", pl1_tracks), playlist("Pl2", pl2_tracks)],
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        orig_add = destination.add_playlist_item

        def fail_on_a1(playlist_id: str, track_id: str) -> None:
            if track_id == "a1":
                raise TemporaryPlatformError("failed_a1")
            orig_add(playlist_id, track_id)

        destination.add_playlist_item = fail_on_a1  # type: ignore[method-assign]

        self.service.execute(job, destination, confirmed=True)

        items = self.service.items.list_for_job(job.id)
        item_a1 = next(it for it in items if it.source_id == "a1")
        item_a2 = next(it for it in items if it.source_id == "a2")
        item_b1 = next(it for it in items if it.source_id == "b1")
        item_b2 = next(it for it in items if it.source_id == "b2")

        # Pl1 was blocked after A1 failed
        self.assertEqual(item_a1.status, ItemStatus.FAILED)
        self.assertEqual(item_a2.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(item_a2.last_error, "playlist_sequence_blocked")
        self.assertEqual(destination.playlist_item_ids("dst-pl1"), [])

        # Pl2 succeeded completely
        self.assertEqual(item_b1.status, ItemStatus.TRANSFERRED)
        self.assertEqual(item_b2.status, ItemStatus.TRANSFERRED)
        self.assertEqual(destination.playlist_item_ids("dst-pl2"), ["b1", "b2"])

    # -------------------------------------------------------------------------
    # Test 23: Runtime failed items retain original write position
    # -------------------------------------------------------------------------
    def test_runtime_failed_items_retain_original_write_position(self) -> None:
        """Test 23: Runtime failed items retain original write position.

        Once assigned during planning, write_position is not cleared or mutated
        when an item transitions from MATCHED to FAILED or AMBIGUOUS.
        """
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        tracks = [track("A", identifier="a"), track("B", identifier="b")]
        source = FakePlatformAdapter(
            display_name="source", playlists=[playlist("Pl1", tracks)]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)

        items = self.service.items.list_for_job(job.id)
        item_a = next(it for it in items if it.source_id == "a")
        item_b = next(it for it in items if it.source_id == "b")

        self.assertEqual(item_a.write_position, 0)
        self.assertEqual(item_b.write_position, 1)

        def fail_all(playlist_id: str, track_id: str) -> None:
            raise TemporaryPlatformError("write_error")

        destination.add_playlist_item = fail_all  # type: ignore[method-assign]

        self.service.execute(job, destination, confirmed=True)

        items_after = self.service.items.list_for_job(job.id)
        item_a_after = next(it for it in items_after if it.source_id == "a")
        item_b_after = next(it for it in items_after if it.source_id == "b")

        self.assertEqual(item_a_after.status, ItemStatus.FAILED)
        self.assertEqual(item_a_after.write_position, 0)

        self.assertEqual(item_b_after.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(item_b_after.write_position, 1)


if __name__ == "__main__":
    unittest.main()


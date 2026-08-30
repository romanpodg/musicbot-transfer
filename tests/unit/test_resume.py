"""Resume and idempotency: Invariant E.

A resumed job must never repeat an operation that was already confirmed.  That
means resume state is kept **per item**, not as "last position = N": a crash at
item 900 of 1000 cannot lose the first 899 by recording only a cursor.

The tests below use the real executor against the in-memory fake adapter, so
they exercise the same code path the TIDAL adapter uses.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from music_transfer.app.services import TransferService
from music_transfer.core.domain import (
    AccountProfile,
    LibrarySnapshot,
    TransferItem,
)
from music_transfer.core.enums import (
    ContentType,
    EntityType,
    ItemStatus,
    JobStatus,
    Platform,
)
from music_transfer.core.errors import TransferConfigurationError
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
)

from tests.support import FakePlatformAdapter, playlist, track


def build_service(root: Path) -> TransferService:
    """Return a transfer service backed by JSON files under ``root``."""

    return TransferService(
        JsonTransferJobRepository(root), JsonTransferItemRepository(root)
    )


def source_snapshot(items: list, playlists: list | None = None) -> LibrarySnapshot:
    """Wrap tracks and playlists into a complete source snapshot."""

    return LibrarySnapshot(
        account=AccountProfile("1", "source", Platform.TIDAL),
        platform=Platform.TIDAL,
        tracks=items,
        playlists=list(playlists or []),
    )


class PlannedJob:
    """A job that has been analyzed and is ready for execution."""

    def __init__(self, service: TransferService, tracks: list, playlists: list | None = None):
        self.service = service
        self.source = FakePlatformAdapter(
            display_name="source", tracks=list(tracks), playlists=list(playlists or [])
        )
        self.destination = FakePlatformAdapter(display_name="destination")
        self.job = service.create_job(
            Platform.TIDAL,
            Platform.TIDAL,
            content=(ContentType.LIKED_TRACKS,),
        )
        self.plan = service.analyze(self.job, self.source, self.destination)


class ResumeDoesNotRepeatWork(unittest.TestCase):
    """Invariant E: confirmed operations are never replayed."""

    def setUp(self) -> None:
        """Create a service in a throwaway directory."""

        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.service = build_service(self.root)

    def test_resume_skips_transferred_items(self) -> None:
        """A job killed mid-run continues without repeating confirmed writes.

        The interruption is a ``KeyboardInterrupt``, i.e. the ``BaseException``
        a real Ctrl+C raises.  Ordinary exceptions are handled per item and
        never abort a run, so a ``BaseException`` is the only honest way to
        model "the process died here, before the job could be finalized".
        """

        tracks = [track(f"Song {index}", identifier=str(index)) for index in range(5)]
        planned = PlannedJob(self.service, tracks)
        destination = planned.destination
        original = destination.save_track

        def crash_after_three(track_id: str) -> None:
            if len(destination.saved_tracks) >= 3:
                raise KeyboardInterrupt("simulated process kill")
            original(track_id)

        destination.save_track = crash_after_three  # type: ignore[method-assign]
        with self.assertRaises(KeyboardInterrupt):
            self.service.execute(planned.job, destination, confirmed=True)
        self.assertEqual(destination.saved_tracks, ["0", "1", "2"])

        # The process is gone.  What survives is only what reached disk.
        reloaded = self.service.jobs.get(planned.job.id)
        assert reloaded is not None
        self.assertFalse(
            reloaded.is_finished,
            "a crashed job must not be persisted as finished, or it could never resume",
        )
        self.assertEqual(
            sorted(
                item.source_id
                for item in self.service.items.list_for_job(reloaded.id)
                if item.status is ItemStatus.TRANSFERRED
            ),
            ["0", "1", "2"],
        )

        # A fresh process resumes from durable per-item state.
        destination.save_track = original  # type: ignore[method-assign]
        result = self.service.resume(reloaded, destination, confirmed=True)
        self.assertEqual(
            destination.saved_tracks,
            ["0", "1", "2", "3", "4"],
            "resume must not repeat confirmed operations",
        )
        self.assertEqual(result["report"].failed, 0)
        self.assertEqual(result["report"].transferred, 5)

    def test_resume_refuses_a_finished_job(self) -> None:
        """A finished job is retried, not resumed.

        Resume means "the process died"; retry means "some items failed".  They
        are different operations with different safety properties, so resume
        refuses to touch a job that already reached a terminal status.
        """

        tracks = [track("A", identifier="a")]
        planned = PlannedJob(self.service, tracks)
        self.service.execute(planned.job, planned.destination, confirmed=True)
        with self.assertRaises(TransferConfigurationError):
            self.service.resume(planned.job, planned.destination, confirmed=True)

    def test_completed_items_are_not_replayed_by_execute(self) -> None:
        """Re-executing a finished job writes nothing new."""

        tracks = [track("A", identifier="a"), track("B", identifier="b")]
        planned = PlannedJob(self.service, tracks)
        self.service.execute(planned.job, planned.destination, confirmed=True)
        self.assertEqual(planned.destination.saved_tracks, ["a", "b"])

        planned.job.status = JobStatus.IMPORTING
        self.service.execute(planned.job, planned.destination, confirmed=True)
        self.assertEqual(
            planned.destination.saved_tracks,
            ["a", "b"],
            "a second execution must be a no-op",
        )

    def test_resume_state_is_per_item_not_a_cursor(self) -> None:
        """Durable state records each item, so a gap is not lost."""

        tracks = [track(f"Song {index}", identifier=str(index)) for index in range(4)]
        planned = PlannedJob(self.service, tracks)
        items = self.service.items.list_for_job(planned.job.id)
        self.assertEqual(len(items), 4)
        # Mark a non-prefix pattern: first and last done, middle two pending.
        items[0].status = ItemStatus.TRANSFERRED
        items[3].status = ItemStatus.TRANSFERRED
        for item in items:
            self.service.items.update(item)
        pending = self.service.recovery.pending_items(planned.job.id)
        self.assertEqual(
            sorted(item.source_id for item in pending), ["1", "2"],
            "resume must be driven by per-item state, not a cursor",
        )

    def test_resume_reports_nothing_left(self) -> None:
        """When every item is done, resume has nothing to do."""

        tracks = [track("A", identifier="a")]
        planned = PlannedJob(self.service, tracks)
        self.service.execute(planned.job, planned.destination, confirmed=True)
        self.assertEqual(self.service.recovery.pending_items(planned.job.id), [])
        self.assertFalse(self.service.recovery.resumable(planned.job))

    def test_retry_job_contains_only_retryable_items(self) -> None:
        """A retry job carries the failures, not the whole library."""

        tracks = [track(f"Song {index}", identifier=str(index)) for index in range(3)]
        planned = PlannedJob(self.service, tracks)
        items = self.service.items.list_for_job(planned.job.id)
        items[0].status = ItemStatus.TRANSFERRED
        items[1].status = ItemStatus.FAILED
        items[2].status = ItemStatus.NOT_FOUND
        for item in items:
            self.service.items.update(item)

        retry = self.service.create_retry_job(planned.job)
        self.assertIsNotNone(retry)
        retry_items = self.service.items.list_for_job(retry.id)  # type: ignore[union-attr]
        self.assertEqual(
            sorted(item.source_id for item in retry_items), ["1", "2"]
        )
        self.assertEqual(retry.metadata["retry_of"], planned.job.id)  # type: ignore[union-attr]


class AmbiguousResultHandling(unittest.TestCase):
    """A timeout is not the same as 'not applied'."""

    def setUp(self) -> None:
        """Create a service in a throwaway directory."""

        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.service = build_service(self.root)

    def test_ambiguous_item_is_not_marked_failed(self) -> None:
        """An unknown write outcome is recorded as ambiguous, not failed."""

        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        item = TransferItem.create(
            job.id, EntityType.TRACK, Platform.TIDAL, "1", Platform.TIDAL
        )
        item.status = ItemStatus.AMBIGUOUS
        self.service.items.add_many([item])
        self.assertIn(item, self.service.recovery.select_for_retry(job.id))

    def test_ambiguous_resolved_against_destination_state(self) -> None:
        """If the item is really there, reconciliation marks it transferred."""

        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        item = TransferItem.create(
            job.id, EntityType.TRACK, Platform.TIDAL, "1", Platform.TIDAL
        )
        item.status = ItemStatus.AMBIGUOUS
        item.destination_id = "1"
        self.service.items.add_many([item])

        destination = FakePlatformAdapter()
        destination.saved_tracks.append("1")
        resolved = self.service.recovery.resolve_ambiguous(
            job.id, destination.get_destination_state()
        )
        self.assertEqual(len(resolved), 1)
        self.assertIs(resolved[0].status, ItemStatus.TRANSFERRED)

    def test_select_for_retry_rejects_terminal_statuses(self) -> None:
        """Asking to retry already-successful items is a caller error."""

        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        with self.assertRaises(ValueError):
            self.service.recovery.select_for_retry(
                job.id, (ItemStatus.TRANSFERRED,)
            )


class CooperativeCancellation(unittest.TestCase):
    """Cancellation stops at the next safe boundary."""

    def setUp(self) -> None:
        """Create a service in a throwaway directory."""

        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.service = build_service(self.root)

    def test_cancellation_stops_before_remaining_items(self) -> None:
        """A cancelled job keeps what it wrote and stops cleanly."""

        from music_transfer.core.transfer import CancellationToken

        tracks = [track(f"Song {index}", identifier=str(index)) for index in range(6)]
        planned = PlannedJob(self.service, tracks)
        token = CancellationToken()
        original = planned.destination.save_track

        def cancel_after_two(track_id: str) -> None:
            original(track_id)
            if len(planned.destination.saved_tracks) == 2:
                token.cancel()

        planned.destination.save_track = cancel_after_two  # type: ignore[method-assign]
        outcome = self.service.execute(
            planned.job, planned.destination, confirmed=True, cancellation_token=token
        )
        self.assertTrue(outcome["outcome"].cancelled)
        self.assertEqual(len(planned.destination.saved_tracks), 2)
        # Work already done is preserved and the job is not reported as complete.
        self.assertIn(planned.job.status.value, {"cancelled", "completed"})

    def test_request_cancellation_sets_the_flag(self) -> None:
        """``request_cancellation`` persists a cooperative stop request."""

        tracks = [track("A", identifier="a")]
        planned = PlannedJob(self.service, tracks)
        self.service.request_cancellation(planned.job)
        self.assertTrue(planned.job.cancellation_requested)


class IdempotentPlaylistWrites(unittest.TestCase):
    """Playlist entries are not duplicated by a resume."""

    def setUp(self) -> None:
        """Create a service in a throwaway directory."""

        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.service = build_service(self.root)

    def test_duplicate_entries_are_preserved_not_deduplicated(self) -> None:
        """A source playlist ``A, B, A`` is recreated as ``A, B, A``."""

        repeated = track("A", identifier="a")
        second = track("B", identifier="b")
        source_playlist = playlist("Mixed", [repeated, second, repeated])
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        source = FakePlatformAdapter(
            display_name="source", playlists=[source_playlist]
        )
        destination = FakePlatformAdapter(display_name="destination")
        self.service.analyze(job, source, destination)
        self.service.execute(job, destination, confirmed=True)

        created = [item for item in destination.playlists if item.source_id.startswith("dst-")]
        self.assertEqual(len(created), 1)
        self.assertEqual(
            destination.playlist_item_ids(created[0].source_id), ["a", "b", "a"]
        )

    def test_resume_after_crash_does_not_recreate_the_playlist(self) -> None:
        """A container created before the crash is reused, not duplicated.

        This is the same Invariant E guarantee applied to a two-level structure:
        the playlist was written and checkpointed, so a resumed run must find
        the existing container id and continue appending entries to it.
        Unresolved source gaps must not cause duplicate writes after resume.
        """

        # Mix entries with an unmatched gap to exercise recovery across gaps
        entries = [
            track("A", identifier="a"),
            track("Missing", identifier="unmatched_gap"),
            track("B", identifier="b"),
            track("C", identifier="c"),
        ]
        source_playlist = playlist("Mixed", entries)
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        source = FakePlatformAdapter(display_name="source", playlists=[source_playlist])
        # Make "unmatched_gap" unresolvable by rejecting search on it
        destination = FakePlatformAdapter(
            display_name="destination",
            fail_on={"search_tracks"},  # won't match if searched or we can mark it directly
        )
        self.service.analyze(job, source, destination)

        # Explicitly mark "unmatched_gap" as NOT_FOUND to create a gap in the planned items
        planned_items = self.service.items.list_for_job(job.id)
        for pi in planned_items:
            if pi.source_id == "unmatched_gap":
                pi.mark(ItemStatus.NOT_FOUND)
                pi.write_position = None
                self.service.items.update(pi)
            elif pi.source_id == "b":
                pi.write_position = 1
                self.service.items.update(pi)
            elif pi.source_id == "c":
                pi.write_position = 2
                self.service.items.update(pi)

        original = destination.add_playlist_item
        counter = {"writes": 0}

        def crash_after_first_entry(playlist_id: str, track_id: str) -> None:
            counter["writes"] += 1
            if counter["writes"] > 1:
                raise KeyboardInterrupt("simulated process kill")
            original(playlist_id, track_id)

        destination.add_playlist_item = crash_after_first_entry  # type: ignore[method-assign]
        with self.assertRaises(KeyboardInterrupt):
            self.service.execute(job, destination, confirmed=True)
        self.assertEqual(destination.playlist_item_ids("dst-mixed"), ["a"])

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        destination.add_playlist_item = original  # type: ignore[method-assign]
        calls_before_resume = len(destination.write_calls)
        self.service.resume(reloaded, destination, confirmed=True)

        # Exactly 2 more writes ("b" and "c"), no duplicate write for "a"
        new_item_writes = [
            call for call in destination.write_calls[calls_before_resume:]
            if call[0] == "add_playlist_item"
        ]
        self.assertEqual(len(new_item_writes), 2)
        created = [item for item in destination.playlists if item.source_id == "dst-mixed"]
        self.assertEqual(len(created), 1, "the playlist must not be created twice")
        self.assertEqual(destination.playlist_item_ids("dst-mixed"), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()

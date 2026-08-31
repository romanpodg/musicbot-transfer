"""Regression coverage for the high-risk TIDAL transfer audit fixes."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import requests
from core.auth import PaginationIntegrityError, TidalLibraryClient, UnsupportedOperationError
from core.backup import BACKUP_FORMAT, BACKUP_VERSION, BackupFormatError, BackupService, _checksum
from core.cleanup import CleanupManager, CleanupScope, DeleteQueue, DeleteQueueStore
from core.models import AccountProfile, LibrarySnapshot, TransferReport
from core.retry import RetryExecutor, RetryPolicy
from core.sorting import SortOrder, sort_items
from core.state import TransferState, TransferStateStore, atomic_write_json
from core.verification import VerificationService

from core.transfer import TransferOptions, TransferService


class PaginationTests(unittest.TestCase):
    def _client(self) -> TidalLibraryClient:
        return TidalLibraryClient(SimpleNamespace(), logging.getLogger("test.pagination"))

    def test_offset_pagination_terminates_at_boundaries_and_over_100(self) -> None:
        for count in (0, 1, 49, 50, 51, 101):
            objects = [SimpleNamespace(id=str(index)) for index in range(count)]
            calls: list[int] = []

            def getter(*, limit: int, offset: int, current_objects=objects, current_calls=calls):
                current_calls.append(offset)
                return current_objects[offset : offset + limit]

            result = self._client()._paginate_offset(getter, operation="test", unique_ids=True)
            self.assertEqual([item.id for item in result], [str(index) for index in range(count)])
            self.assertLessEqual(len(calls), count // 50 + 2)

    def test_repeated_full_folder_page_is_stopped_without_duplicates(self) -> None:
        page = [SimpleNamespace(id=str(index)) for index in range(50)]
        with self.assertRaises(PaginationIntegrityError):
            self._client()._paginate_offset(lambda **_: page, operation="folders", unique_ids=True)

    def test_rate_limit_retry_after_is_not_shortened(self) -> None:
        response = SimpleNamespace(status_code=429, headers={"Retry-After": "13"})
        error = requests.HTTPError(response=response)
        sleeps: list[float] = []
        attempts = 0

        def action() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise error

        RetryExecutor(logging.getLogger("test.retry_after"), RetryPolicy(max_attempts=2), sleep=sleeps.append).call("read", action)
        self.assertEqual(sleeps, [13])


class PlaylistAndResumeTests(unittest.TestCase):
    def _state(self) -> TransferState:
        return TransferState.create("transfer", LibrarySnapshot(account=AccountProfile("source", "source")))

    def test_server_prefix_is_authoritative_when_checkpoint_is_ahead_or_behind(self) -> None:
        expected = [{"kind": "track", "id": value} for value in ("a", "b", "c")]
        destination = SimpleNamespace(playlist_media_order=lambda _: expected[:2])
        self.assertEqual(TransferService._reconcile_playlist_position(destination, "d", "s", expected, 3), 2)
        self.assertEqual(TransferService._reconcile_playlist_position(destination, "d", "s", expected, 1), 2)

    def test_playlist_order_conflict_is_rejected(self) -> None:
        expected = [{"kind": "track", "id": value} for value in ("a", "b")]
        destination = SimpleNamespace(playlist_media_order=lambda _: list(reversed(expected)))
        with self.assertRaisesRegex(Exception, "playlist_resume_mismatch"):
            TransferService._reconcile_playlist_position(destination, "d", "s", expected, 2)

    def test_video_is_terminal_and_following_track_continues(self) -> None:
        class Destination:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def playlist_media_order(self, _: str):
                return []

            def add_playlist_item(self, _: str, kind: str, item_id: str) -> None:
                if kind == "video":
                    raise UnsupportedOperationError("playlist_video_add_unsupported")
                self.calls.append((kind, item_id))

        with tempfile.TemporaryDirectory() as directory:
            store = TransferStateStore(Path(directory) / "state.json")
            service = TransferService(store, logging.getLogger("test.playlist"))
            state, report, destination = self._state(), TransferReport("transfer"), Destination()
            service._transfer_playlist_items(
                destination, state, report, "source", "dest",
                {"item_order": [{"kind": "track", "id": "a"}, {"kind": "video", "id": "v"}, {"kind": "track", "id": "b"}]},
            )
            self.assertEqual(destination.calls, [("track", "a"), ("track", "b")])
            self.assertEqual(len(report.unsupported_items), 1)
            self.assertEqual(state.current_position, 3)

    def test_same_account_transfer_is_blocked_but_restore_is_allowed(self) -> None:
        class Destination:
            def profile(self):
                return AccountProfile("same", "same")

            def favorite_ids(self, _: str):
                return set()

        with tempfile.TemporaryDirectory() as directory:
            service = TransferService(TransferStateStore(Path(directory) / "state.json"), logging.getLogger("test.account"))
            snapshot = LibrarySnapshot(account=AccountProfile("same", "same"))
            with self.assertRaisesRegex(Exception, "same_source_destination_account"):
                service.run(Destination(), TransferState.create("transfer", snapshot), TransferOptions(), confirmed=True)
            # Empty restore reaches normal completion without performing a mutation.
            service.run(Destination(), TransferState.create("restore", snapshot), TransferOptions(), confirmed=True)

    def test_resume_reconciles_source_positions_across_terminal_video_gap(self) -> None:
        state = self._state()
        state.mark_completed("playlist_items", "p:0")
        state.mark_terminal("playlist_items", "p:1", "unsupported")
        destination = SimpleNamespace(playlist_media_order=lambda _: [
            {"kind": "track", "id": "A"}, {"kind": "track", "id": "B"}
        ])
        position = TransferService._reconcile_playlist_position(
            destination, "d", "p",
            [{"kind": "track", "id": "A"}, {"kind": "video", "id": "V"}, {"kind": "track", "id": "B"}, {"kind": "track", "id": "C"}],
            2, state,
        )
        self.assertEqual(position, 3)
        self.assertEqual(state.status_of("playlist_items", "p:2"), "completed")

    def test_terminal_favorites_do_not_retry_but_retryable_does(self) -> None:
        class Destination:
            def __init__(self) -> None:
                self.calls: list[str] = []
            def favorite_ids(self, _: str): return set()
            def add_favorite(self, _, item_id: str): self.calls.append(item_id)

        with tempfile.TemporaryDirectory() as directory:
            service = TransferService(TransferStateStore(Path(directory) / "state.json"), logging.getLogger("test.terminal"))
            state = TransferState.create("restore", LibrarySnapshot(account=AccountProfile("s", "s"), tracks=[{"id": value} for value in ("u", "p", "x", "r")]))
            state.mark_terminal("tracks", "u", "unavailable")
            state.mark_terminal("tracks", "p", "failed_permanent")
            state.mark_terminal("tracks", "x", "already_present")
            state.mark_failed("tracks", "r", 1)
            destination = Destination()
            service.run(destination, state, TransferOptions(), confirmed=True)
            self.assertEqual(destination.calls, ["r"])

    def test_status_replay_is_canonical_and_latest_transition_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransferStateStore(Path(directory) / "state.json")
            state = TransferState.create("restore", LibrarySnapshot(account=AccountProfile("s", "s")))
            state.mark_ambiguous("tracks", "one")
            store.save(state)
            self.assertTrue(TransferStateStore(store.path).load().is_ambiguous("tracks", "one"))
            state.mark_completed("tracks", "one")
            store.save(state)
            state.mark_failed("tracks", "one", 1)
            store.save(state)
            state.mark_completed("tracks", "one")
            store.save(state)
            loaded = TransferStateStore(store.path).load()
            self.assertTrue(loaded.is_completed("tracks", "one"))
            self.assertFalse(loaded.is_ambiguous("tracks", "one"))

    def test_create_intent_is_persisted_before_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransferStateStore(Path(directory) / "state.json")
            state = TransferState.create("restore", LibrarySnapshot(account=AccountProfile("s", "s")))
            state.mark_create_intent("playlists", "p", {"name": "My Music", "parent_id": "root", "baseline_ids": ["old"]})
            store.save(state)
            loaded = TransferStateStore(store.path).load()
            self.assertEqual(loaded.status_of("playlists", "p"), "creating")
            self.assertEqual(loaded.create_intents["playlists:p"]["baseline_ids"], ["old"])


class SortingVerificationAndCleanupTests(unittest.TestCase):
    def test_missing_dates_are_last_in_both_directions_and_artist_uses_name(self) -> None:
        items = [
            {"id": "missing", "name": "Zulu"}, {"id": "old", "name": "alpha", "added_at": "2024-01-01"},
            {"id": "new", "name": "Beta", "added_at": "2025-01-01"},
        ]
        self.assertEqual([item["id"] for item in sort_items(items, SortOrder.NEWEST_FIRST)], ["new", "old", "missing"])
        self.assertEqual([item["id"] for item in sort_items(items, SortOrder.OLDEST_FIRST)], ["old", "new", "missing"])
        self.assertEqual([item["id"] for item in sort_items(items, SortOrder.ALPHABETICAL)], ["old", "new", "missing"])

    def test_cleanup_verifies_original_targets_not_new_content(self) -> None:
        class Client:
            def export_library(self, _=None):
                return LibrarySnapshot(account=AccountProfile("x", "x"), tracks=[{"id": "new"}])

        verification = CleanupManager(logging.getLogger("test.cleanup")).verify_cleanup(
            Client(), CleanupScope.TRACKS, [{"category": "tracks", "id": "removed"}]
        )
        self.assertEqual(verification.remaining, 0)
        self.assertEqual(verification.new_counts, {"tracks": 1})

    def test_verification_detects_equal_count_wrong_ids(self) -> None:
        source = LibrarySnapshot(account=AccountProfile("s", "s"), tracks=[{"id": "a"}, {"id": "b"}])
        destination = SimpleNamespace(export_library=lambda _=None: LibrarySnapshot(account=AccountProfile("d", "d"), tracks=[{"id": "x"}, {"id": "y"}]))
        with tempfile.TemporaryDirectory() as directory:
            report = TransferReport("transfer")
            service = VerificationService(Path(directory) / "report.json")
            service.verify_and_write(source, destination, report)
            data = (Path(directory) / "report.json").read_text(encoding="utf-8")
            self.assertIn('"missing": 2', data)

    def test_transfer_journal_replays_small_events_without_rewriting_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransferStateStore(Path(directory) / "transfer.json")
            snapshot = LibrarySnapshot(account=AccountProfile("s", "s"), tracks=[{"id": str(index)} for index in range(10_000)])
            state = TransferState.create("restore", snapshot)
            store.save(state)
            base_size = store.path.stat().st_size
            for index in range(100):
                state.mark_completed("tracks", str(index))
                store.save(state)
            self.assertEqual(store.path.stat().st_size, base_size)
            self.assertLess(store.journal_path.stat().st_size, base_size // 4)
            self.assertTrue(TransferStateStore(store.path).load().is_completed("tracks", "99"))

    def test_backup_rejects_inconsistent_complete_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.json"
            snapshot = LibrarySnapshot(account=AccountProfile("s", "s"), incomplete_sections=["tracks"])
            library = snapshot.as_dict()
            atomic_write_json(path, {"format": BACKUP_FORMAT, "format_version": BACKUP_VERSION, "status": "complete", "source_account_id": "s", "counts": snapshot.counts(), "checksum_sha256": _checksum(library), "library": library})
            with self.assertRaises(BackupFormatError):
                BackupService(path).load(path)

    def test_missing_playlist_mapping_is_a_verification_failure(self) -> None:
        source = LibrarySnapshot(account=AccountProfile("s", "s"), playlists=[{"id": "p", "is_owned": True, "item_order": []}])
        destination = SimpleNamespace(export_library=lambda _=None: LibrarySnapshot(account=AccountProfile("d", "d")))
        with tempfile.TemporaryDirectory() as directory:
            report = TransferReport("transfer")
            VerificationService(Path(directory) / "r.json").verify_and_write(source, destination, report, TransferState.create("transfer", source))
            self.assertEqual(report.verification_outcome, "failed")

    def test_delete_queue_uses_constant_size_journal_for_large_state_transitions(self) -> None:
        """Guard against accidentally serializing a 50k-item queue per delete."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            queue = DeleteQueue("delete_tracks", [
                {"category": "tracks", "id": str(index), "title": "", "artist": "", "is_owned": False,
                 "status": "pending", "attempts": 0, "reason": ""}
                for index in range(10_000)
            ])
            store = DeleteQueueStore(path)
            store.save(queue)
            snapshot_size = path.stat().st_size
            for item in queue.items[:100]:
                item["status"] = "completed"
                store.record(queue, item)
            self.assertLess(store.journal_path.stat().st_size, snapshot_size // 10)
            self.assertEqual(sum(item["status"] == "completed" for item in store.load().items), 100)

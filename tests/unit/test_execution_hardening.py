"""Regression tests for execution semantics and fatal error handling.

Covers:
1. AMBIGUOUS item is never executed
2. NOT_FOUND item is never executed
3. UNAVAILABLE item is never executed
4. ALREADY_EXISTS item is not written again
5. TRANSFERRED item is not executed twice
6. MATCHED item with valid operation executes once
7. CREATE_PLAYLIST operation can execute
8. Authentication abort marks job FAILED
9. Authorization abort marks job FAILED
10. Partial success before auth failure is preserved
11. Fatal abort never becomes COMPLETED
12. Credential scrubbing prevents token leakage in abort reasons and logs
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from music_transfer.app.services import TransferService
from music_transfer.core.domain import TransferItem
from music_transfer.core.enums import (
    ContentType,
    EntityType,
    ItemStatus,
    JobStatus,
    Platform,
    TransferOperation,
)
from music_transfer.core.errors import AuthenticationError, AuthorizationError
from music_transfer.core.transfer import scrub_credentials
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
)

from tests.support import FakePlatformAdapter


def build_service(root: Path) -> TransferService:
    return TransferService(
        JsonTransferJobRepository(root), JsonTransferItemRepository(root)
    )


class ExecutionHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = build_service(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _get_item(self, job_id: str, item_id: str) -> TransferItem:
        items = self.service.items.list_for_job(job_id)
        for it in items:
            if it.id == item_id:
                return it
        raise KeyError(f"Item not found: {item_id}")

    def test_ambiguous_item_is_never_executed(self) -> None:
        """AMBIGUOUS items must not trigger destination writes or be rewritten as NOT_FOUND."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        destination = FakePlatformAdapter(display_name="destination")

        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-ambiguous",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.mark(ItemStatus.AMBIGUOUS, error="search_ambiguous")
        self.service.items.add_many([item])

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        outcome = self.service.execute(job, destination, confirmed=True)["outcome"]
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(outcome.succeeded, 0)
        self.assertEqual(len(destination.saved_tracks), 0)

        persisted = self._get_item(job.id, item.id)
        self.assertEqual(persisted.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(persisted.attempt_count, 0)

    def test_not_found_item_is_never_executed(self) -> None:
        """NOT_FOUND items must be skipped and never reach destination mutation."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        destination = FakePlatformAdapter(display_name="destination")

        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-not-found",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.mark(ItemStatus.NOT_FOUND, error="track_not_found")
        self.service.items.add_many([item])

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        outcome = self.service.execute(job, destination, confirmed=True)["outcome"]
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(outcome.succeeded, 0)
        self.assertEqual(len(destination.saved_tracks), 0)

        persisted = self._get_item(job.id, item.id)
        self.assertEqual(persisted.status, ItemStatus.NOT_FOUND)
        self.assertEqual(persisted.attempt_count, 0)

    def test_unavailable_item_is_never_executed(self) -> None:
        """UNAVAILABLE items must never be executed."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        destination = FakePlatformAdapter(display_name="destination")

        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-unavailable",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.mark(ItemStatus.UNAVAILABLE, error="rights_restriction")
        self.service.items.add_many([item])

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        outcome = self.service.execute(job, destination, confirmed=True)["outcome"]
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(outcome.succeeded, 0)
        self.assertEqual(len(destination.saved_tracks), 0)

        persisted = self._get_item(job.id, item.id)
        self.assertEqual(persisted.status, ItemStatus.UNAVAILABLE)
        self.assertEqual(persisted.attempt_count, 0)

    def test_already_existing_item_is_not_written_again(self) -> None:
        """ALREADY_EXISTS items are terminal and must be skipped without destination writes."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        destination = FakePlatformAdapter(display_name="destination")

        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-existing",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.destination_id = "dst-existing"
        item.mark(ItemStatus.ALREADY_EXISTS)
        self.service.items.add_many([item])

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        outcome = self.service.execute(job, destination, confirmed=True)["outcome"]
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(len(destination.saved_tracks), 0)

        persisted = self._get_item(job.id, item.id)
        self.assertEqual(persisted.status, ItemStatus.ALREADY_EXISTS)
        self.assertEqual(persisted.attempt_count, 0)

    def test_transferred_item_is_not_executed_twice(self) -> None:
        """TRANSFERRED items are terminal and must never be re-written."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        destination = FakePlatformAdapter(display_name="destination")

        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-done",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.destination_id = "dst-done"
        item.mark(ItemStatus.TRANSFERRED)
        self.service.items.add_many([item])

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        outcome = self.service.execute(job, destination, confirmed=True)["outcome"]
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(len(destination.saved_tracks), 0)

        persisted = self._get_item(job.id, item.id)
        self.assertEqual(persisted.status, ItemStatus.TRANSFERRED)
        self.assertEqual(persisted.attempt_count, 0)

    def test_matched_item_with_valid_operation_executes_once(self) -> None:
        """MATCHED item with valid operation and destination_id executes and marks TRANSFERRED."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        destination = FakePlatformAdapter(display_name="destination")

        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-1",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.destination_id = "dst-1"
        item.mark(ItemStatus.MATCHED)
        self.service.items.add_many([item])

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        outcome = self.service.execute(job, destination, confirmed=True)["outcome"]
        self.assertEqual(outcome.succeeded, 1)
        self.assertEqual(outcome.failed, 0)
        self.assertEqual(destination.saved_tracks, ["dst-1"])

        persisted = self._get_item(job.id, item.id)
        self.assertEqual(persisted.status, ItemStatus.TRANSFERRED)
        self.assertEqual(persisted.attempt_count, 1)

    def test_create_playlist_operation_can_execute(self) -> None:
        """CREATE_PLAYLIST operation executes when destination_id is None and metadata exists."""
        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,)
        )
        destination = FakePlatformAdapter(display_name="destination")

        item = TransferItem.create(
            job.id,
            EntityType.PLAYLIST,
            Platform.TIDAL,
            "source-pl-1",
            Platform.TIDAL,
            source_metadata={"name": "My Favorites"},
            operation=TransferOperation.CREATE_PLAYLIST,
        )
        self.service.items.add_many([item])

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        outcome = self.service.execute(job, destination, confirmed=True)["outcome"]
        self.assertEqual(outcome.succeeded, 1)

        persisted = self._get_item(job.id, item.id)
        self.assertEqual(persisted.status, ItemStatus.TRANSFERRED)
        self.assertEqual(persisted.destination_id, "dst-source-pl-1")

    def test_authentication_abort_marks_job_failed(self) -> None:
        """AuthenticationError during execution halts immediately and marks job FAILED."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        destination = FakePlatformAdapter(
            display_name="destination",
            fail_on={"save_track"},
            error_factory=lambda: AuthenticationError(message="token_expired: access_token=secret123"),
        )

        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-auth-fail",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.destination_id = "dst-auth-fail"
        item.mark(ItemStatus.MATCHED)
        self.service.items.add_many([item])

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        result = self.service.execute(job, destination, confirmed=True)
        outcome = result["outcome"]
        self.assertTrue(outcome.aborted)
        self.assertEqual(outcome.abort_error, "authentication_error")
        self.assertEqual(result["verification"], {})

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.FAILED)
        self.assertEqual(reloaded.error_code, "authentication_error")
        self.assertIsNotNone(reloaded.finished_at)

    def test_authorization_abort_marks_job_failed(self) -> None:
        """AuthorizationError during execution halts immediately and marks job FAILED."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        destination = FakePlatformAdapter(
            display_name="destination",
            fail_on={"save_track"},
            error_factory=lambda: AuthorizationError(message="insufficient_scope: Bearer xyz123"),
        )

        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-authz-fail",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.destination_id = "dst-authz-fail"
        item.mark(ItemStatus.MATCHED)
        self.service.items.add_many([item])

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        result = self.service.execute(job, destination, confirmed=True)
        outcome = result["outcome"]
        self.assertTrue(outcome.aborted)
        self.assertEqual(outcome.abort_error, "authorization_error")
        self.assertEqual(result["verification"], {})

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.FAILED)
        self.assertEqual(reloaded.error_code, "authorization_error")

    def test_partial_success_before_auth_failure_is_preserved(self) -> None:
        """Successful items prior to authentication abort remain checkpointed as TRANSFERRED."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)

        saved = []

        def save_track_hook(track_id: str) -> None:
            if track_id == "dst-3":
                raise AuthenticationError(message="session_invalidated")
            saved.append(track_id)

        destination = FakePlatformAdapter(display_name="destination")
        destination.save_track = save_track_hook  # type: ignore[method-assign]

        items = []
        for i in (1, 2, 3, 4):
            item = TransferItem.create(
                job.id,
                EntityType.TRACK,
                Platform.TIDAL,
                f"track-{i}",
                Platform.TIDAL,
                original_position=i,
                operation=TransferOperation.SAVE_TRACK,
            )
            item.destination_id = f"dst-{i}"
            item.mark(ItemStatus.MATCHED)
            items.append(item)

        self.service.items.add_many(items)

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        result = self.service.execute(job, destination, confirmed=True)
        outcome = result["outcome"]

        self.assertTrue(outcome.aborted)
        self.assertEqual(outcome.succeeded, 2)
        self.assertEqual(saved, ["dst-1", "dst-2"])

        reloaded_items = {i.source_id: i for i in self.service.items.list_for_job(job.id)}
        self.assertEqual(reloaded_items["track-1"].status, ItemStatus.TRANSFERRED)
        self.assertEqual(reloaded_items["track-2"].status, ItemStatus.TRANSFERRED)
        self.assertEqual(reloaded_items["track-3"].status, ItemStatus.FAILED)
        self.assertEqual(reloaded_items["track-4"].status, ItemStatus.MATCHED)  # Untouched

        reloaded_job = self.service.jobs.get(job.id)
        assert reloaded_job is not None
        self.assertEqual(reloaded_job.status, JobStatus.FAILED)

    def test_fatal_abort_never_becomes_completed(self) -> None:
        """A fatal execution abort must never transition through VERIFYING into COMPLETED."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        destination = FakePlatformAdapter(
            display_name="destination",
            fail_on={"save_track"},
            error_factory=lambda: AuthenticationError(message="token_revoked"),
        )

        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-fatal",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.destination_id = "dst-fatal"
        item.mark(ItemStatus.MATCHED)
        self.service.items.add_many([item])

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        result = self.service.execute(job, destination, confirmed=True)
        report = result["report"]

        reloaded_job = self.service.jobs.get(job.id)
        assert reloaded_job is not None

        self.assertNotEqual(reloaded_job.status, JobStatus.COMPLETED)
        self.assertNotEqual(reloaded_job.status, JobStatus.VERIFYING)
        self.assertEqual(reloaded_job.status, JobStatus.FAILED)
        self.assertTrue(any("aborted:authentication_error" in w for w in report.warnings))

    def test_credential_scrubbing_prevents_token_leakage(self) -> None:
        """Credentials in error messages are redacted from outcome abort_reason and logs."""
        raw_msg = (
            "401 Unauthorized: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-ID "
            "access_token=secret_token_value refresh_token: super_secret_refresh password=mysecret"
        )
        scrubbed = scrub_credentials(raw_msg)
        assert scrubbed is not None
        self.assertNotIn("secret_token_value", scrubbed)
        self.assertNotIn("super_secret_refresh", scrubbed)
        self.assertNotIn("mysecret", scrubbed)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", scrubbed)
        self.assertIn("[REDACTED]", scrubbed)


if __name__ == "__main__":
    unittest.main()

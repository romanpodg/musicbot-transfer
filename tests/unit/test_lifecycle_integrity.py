"""Phase 1.3A regression tests: lifecycle integrity and verification semantics.

Verifies:
1. TransferJob state machine is authoritative and non-bypassable by application services.
2. Terminal states (COMPLETED, FAILED, CANCELLED) cannot transition to any other status.
3. Idempotent cancel and fail semantics.
4. Resume follows legal transitions and refuses terminal jobs.
5. Dry-run transitions IMPORTING -> COMPLETED without fake verification success (NOT_RUN).
6. Post-write verification outcome is distinct from JobStatus (PASSED, FAILED, PARTIAL, NOT_RUN).
7. Verification mismatches (missing items, unexpected items, playlist order mismatches) produce FAILED.
8. Unsupported/incomplete destination state produces PARTIAL (never PASSED).
9. Verification does not rewrite historical TransferItem statuses.
10. Fatal authentication/authorization errors during verification transition VERIFYING -> FAILED.
11. Backward compatibility: legacy serialized jobs load verification_status as NOT_RUN.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from music_transfer.app.dto import report_view
from music_transfer.app.services.transfer_service import TransferService
from music_transfer.core.domain import (
    Account,
    TransferItem,
    TransferJob,
    TransferPlan,
    TransferPlanItem,
    TransferSettings,
    VerificationResult,
)
from music_transfer.core.enums import (
    ContentType,
    EntityType,
    ItemStatus,
    JobStatus,
    Platform,
    TransferOperation,
    VerificationStatus,
)
from music_transfer.core.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidPersistedStateError,
    InvalidStateTransition,
    UnsupportedCapabilityError,
)
from music_transfer.core.ports import DestinationState
from music_transfer.core.transfer import (
    CancellationToken,
    aggregate_verification_status,
    transition,
)
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
)

from tests.support import FakePlatformAdapter, track


def build_service(root: Path) -> TransferService:
    return TransferService(
        JsonTransferJobRepository(root), JsonTransferItemRepository(root)
    )


def new_account(identifier: str = "acc-1") -> Account:
    return Account.create(Platform.TIDAL, identifier, "Test Account")


def confirm_job_items(service: TransferService, job: TransferJob) -> None:
    items = service.items.list_for_job(job.id)
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
        )
        for it in items
    )
    plan = TransferPlan.create(job.id, revision=1, items=plan_items)
    service.plans.save(plan)
    job.active_plan_id = plan.plan_id
    job.active_plan_revision = plan.revision
    job.active_plan_hash = plan.plan_hash
    service.jobs.update(job)
    service.confirm_plan(
        job,
        plan_id=plan.plan_id,
        revision=plan.revision,
        plan_hash=plan.plan_hash,
    )


class LifecycleIntegrityServiceTests(unittest.TestCase):
    """Application-level lifecycle transition enforcement in TransferService."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = build_service(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_and_plan_job(
        self,
        tracks: list[Any] | None = None,
        settings: TransferSettings | None = None,
    ) -> tuple[TransferJob, FakePlatformAdapter]:
        destination = FakePlatformAdapter()
        job = self.service.create_job(
            new_account("src-1"),
            new_account("dst-1"),
            settings=settings,
        )
        sample_tracks = tracks or [track("Song 1", identifier="s1")]
        source = FakePlatformAdapter(tracks=sample_tracks)
        self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=job.active_plan_id,
            revision=job.active_plan_revision,
            plan_hash=job.active_plan_hash,
        )
        return job, destination

    # -- Section 25: Terminal state & transition enforcement -------------------

    def test_service_cannot_cancel_completed_job(self) -> None:
        """A completed job cannot be cancelled via TransferService."""
        job, destination = self._create_and_plan_job()
        self.service.execute(job, destination, confirmed=True)
        self.assertEqual(job.status, JobStatus.COMPLETED)

        with self.assertRaises(InvalidStateTransition) as ctx:
            self.service.cancel(job)
        self.assertEqual(ctx.exception.current, JobStatus.COMPLETED.value)
        self.assertEqual(ctx.exception.target, JobStatus.CANCELLED.value)

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.COMPLETED)

    def test_service_cannot_fail_completed_job(self) -> None:
        """A completed job cannot be failed via TransferService."""
        job, destination = self._create_and_plan_job()
        self.service.execute(job, destination, confirmed=True)
        self.assertEqual(job.status, JobStatus.COMPLETED)

        with self.assertRaises(InvalidStateTransition) as ctx:
            self.service.fail(job, "late_error")
        self.assertEqual(ctx.exception.current, JobStatus.COMPLETED.value)
        self.assertEqual(ctx.exception.target, JobStatus.FAILED.value)

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.COMPLETED)

    def test_failed_job_cannot_resume(self) -> None:
        """A failed job cannot be resumed via TransferService.resume()."""
        job, destination = self._create_and_plan_job()
        self.service.fail(job, "network_down")
        self.assertEqual(job.status, JobStatus.FAILED)

        with self.assertRaises(InvalidStateTransition) as ctx:
            self.service.resume(job, destination, confirmed=True)
        self.assertEqual(ctx.exception.current, JobStatus.FAILED.value)
        self.assertEqual(ctx.exception.target, JobStatus.IMPORTING.value)

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.FAILED)

    def test_cancelled_job_cannot_resume(self) -> None:
        """A cancelled job cannot be resumed via TransferService.resume()."""
        job, destination = self._create_and_plan_job()
        self.service.cancel(job)
        self.assertEqual(job.status, JobStatus.CANCELLED)

        with self.assertRaises(InvalidStateTransition) as ctx:
            self.service.resume(job, destination, confirmed=True)
        self.assertEqual(ctx.exception.current, JobStatus.CANCELLED.value)
        self.assertEqual(ctx.exception.target, JobStatus.IMPORTING.value)

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.CANCELLED)

    def test_paused_job_resumes_through_legal_transition(self) -> None:
        """A paused job legally transitions PAUSED -> IMPORTING -> VERIFYING -> COMPLETED."""
        job, destination = self._create_and_plan_job()
        # Move job to PAUSED
        transition(job, JobStatus.IMPORTING)
        transition(job, JobStatus.PAUSED)
        self.service.jobs.update(job)
        self.assertEqual(job.status, JobStatus.PAUSED)

        result = self.service.resume(job, destination, confirmed=True)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(result["verification_status"], VerificationStatus.PASSED)

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.COMPLETED)
        self.assertEqual(reloaded.verification_status, VerificationStatus.PASSED)

    def test_authentication_error_during_resume_destination_state_is_fatal(self) -> None:
        """AuthenticationError during resume destination state recovery transitions job to FAILED."""
        job, _ = self._create_and_plan_job()
        transition(job, JobStatus.IMPORTING)
        transition(job, JobStatus.PAUSED)
        self.service.jobs.update(job)

        def auth_error_state(sections=None) -> DestinationState:
            raise AuthenticationError(code="auth_revoked", message="Session expired")

        destination = FakePlatformAdapter()
        destination.get_destination_state = auth_error_state  # type: ignore[method-assign]

        result = self.service.resume(job, destination, confirmed=True)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, "auth_revoked")
        self.assertEqual(job.verification_status, VerificationStatus.NOT_RUN)
        self.assertEqual(result["verification_status"], VerificationStatus.NOT_RUN)
        self.assertTrue(result["outcome"].aborted)
        self.assertEqual(result["outcome"].abort_error, "auth_revoked")
        self.assertIsNotNone(job.finished_at)

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.FAILED)
        self.assertEqual(reloaded.error_code, "auth_revoked")
        self.assertEqual(reloaded.verification_status, VerificationStatus.NOT_RUN)

    def test_authorization_error_during_resume_destination_state_is_fatal(self) -> None:
        """AuthorizationError during resume destination state recovery transitions job to FAILED."""
        job, _ = self._create_and_plan_job()
        transition(job, JobStatus.IMPORTING)
        transition(job, JobStatus.PAUSED)
        self.service.jobs.update(job)

        def authz_error_state(sections=None) -> DestinationState:
            raise AuthorizationError(code="scope_missing", message="Missing permission")

        destination = FakePlatformAdapter()
        destination.get_destination_state = authz_error_state  # type: ignore[method-assign]

        result = self.service.resume(job, destination, confirmed=True)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, "scope_missing")
        self.assertEqual(job.verification_status, VerificationStatus.NOT_RUN)
        self.assertEqual(result["verification_status"], VerificationStatus.NOT_RUN)
        self.assertTrue(result["outcome"].aborted)
        self.assertEqual(result["outcome"].abort_error, "scope_missing")
        self.assertIsNotNone(job.finished_at)

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.FAILED)
        self.assertEqual(reloaded.error_code, "scope_missing")
        self.assertEqual(reloaded.verification_status, VerificationStatus.NOT_RUN)

    def test_resume_auth_failure_with_no_executable_items_never_completes(self) -> None:
        """A resume run with only ambiguous/non-executable items transitions to FAILED upon auth error, never COMPLETED."""
        job = self.service.create_job(new_account("src-amb"), new_account("dst-amb"))
        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-amb",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.mark(ItemStatus.AMBIGUOUS)
        self.service.items.add_many([item])
        # Position job in PAUSED state
        job.status = JobStatus.PAUSED
        self.service.jobs.update(job)

        def auth_error_state(sections=None) -> DestinationState:
            raise AuthenticationError(code="token_expired", message="Token expired")

        destination = FakePlatformAdapter()
        destination.get_destination_state = auth_error_state  # type: ignore[method-assign]

        result = self.service.resume(job, destination, confirmed=True)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertNotEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.error_code, "token_expired")
        self.assertEqual(job.verification_status, VerificationStatus.NOT_RUN)
        self.assertEqual(result["verification_status"], VerificationStatus.NOT_RUN)

    def test_completed_job_remains_terminal(self) -> None:
        """All attempts to transition out of COMPLETED are rejected."""
        job, destination = self._create_and_plan_job()
        self.service.execute(job, destination, confirmed=True)
        self.assertEqual(job.status, JobStatus.COMPLETED)

        # Cannot execute
        with self.assertRaises(InvalidStateTransition):
            self.service.execute(job, destination, confirmed=True)

        # Cannot resume
        with self.assertRaises(InvalidStateTransition):
            self.service.resume(job, destination, confirmed=True)

        # Cannot cancel
        with self.assertRaises(InvalidStateTransition):
            self.service.cancel(job)

        # Cannot fail
        with self.assertRaises(InvalidStateTransition):
            self.service.fail(job, "err")

    # -- Section 26: Idempotent terminal commands -----------------------------

    def test_cancel_cancelled_job_is_idempotent(self) -> None:
        """Calling cancel on an already CANCELLED job is an idempotent no-op."""
        job, _ = self._create_and_plan_job()
        self.service.cancel(job)
        self.assertEqual(job.status, JobStatus.CANCELLED)
        finished_at = job.finished_at

        # Repeated cancel
        ret = self.service.cancel(job)
        self.assertEqual(ret.status, JobStatus.CANCELLED)
        self.assertEqual(ret.finished_at, finished_at)

    def test_fail_failed_job_is_idempotent(self) -> None:
        """Calling fail on an already FAILED job preserves original error code."""
        job, _ = self._create_and_plan_job()
        self.service.fail(job, "original_error")
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, "original_error")
        finished_at = job.finished_at

        # Repeated fail does not rewrite original error
        ret = self.service.fail(job, "secondary_error")
        self.assertEqual(ret.status, JobStatus.FAILED)
        self.assertEqual(ret.error_code, "original_error")
        self.assertEqual(ret.finished_at, finished_at)

    def test_cannot_cancel_failed_job(self) -> None:
        """A FAILED job cannot be mutated into CANCELLED."""
        job, _ = self._create_and_plan_job()
        self.service.fail(job, "error")
        with self.assertRaises(InvalidStateTransition):
            self.service.cancel(job)

    def test_cannot_fail_cancelled_job(self) -> None:
        """A CANCELLED job cannot be mutated into FAILED."""
        job, _ = self._create_and_plan_job()
        self.service.cancel(job)
        with self.assertRaises(InvalidStateTransition):
            self.service.fail(job, "error")

    # -- Section 23: Timestamps & resume preservation -------------------------

    def test_resume_preserves_original_started_at(self) -> None:
        """Resuming an interrupted run does not overwrite the original started_at timestamp."""
        job, destination = self._create_and_plan_job()
        transition(job, JobStatus.IMPORTING)
        job.started_at = "2026-01-01T00:00:00+00:00"
        transition(job, JobStatus.PAUSED)
        self.service.jobs.update(job)

        self.service.resume(job, destination, confirmed=True)
        self.assertEqual(job.started_at, "2026-01-01T00:00:00+00:00")
        self.assertIsNotNone(job.finished_at)


class VerificationSemanticsTests(unittest.TestCase):
    """Destination post-write verification semantics and status tracking."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = build_service(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _setup_job_with_items(
        self,
        item_ids: list[str],
        entity_type: EntityType = EntityType.TRACK,
        operation: TransferOperation = TransferOperation.SAVE_TRACK,
    ) -> tuple[TransferJob, FakePlatformAdapter]:
        job = self.service.create_job(new_account("src-v"), new_account("dst-v"))
        destination = FakePlatformAdapter()
        items = []
        for i, src_id in enumerate(item_ids):
            item = TransferItem.create(
                job.id,
                entity_type,
                Platform.TIDAL,
                src_id,
                Platform.TIDAL,
                original_position=i,
                write_position=i,
                operation=operation,
            )
            item.destination_id = f"dst-{src_id}"
            item.mark(ItemStatus.MATCHED)
            items.append(item)
        self.service.items.add_many(items)
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
            )
            for it in items
        )
        plan = TransferPlan.create(job.id, revision=1, items=plan_items)
        self.service.plans.save(plan)
        job.active_plan_id = plan.plan_id
        job.active_plan_revision = plan.revision
        job.active_plan_hash = plan.plan_hash
        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )
        return job, destination

    # -- Section 27: Verification status outcomes -----------------------------

    def test_successful_verification_sets_passed(self) -> None:
        """When destination contains all expected items, verification is PASSED."""
        job, destination = self._setup_job_with_items(["track-1", "track-2"])
        result = self.service.execute(job, destination, confirmed=True)

        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.verification_status, VerificationStatus.PASSED)
        self.assertEqual(result["verification_status"], VerificationStatus.PASSED)
        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.verification_status, VerificationStatus.PASSED)

    def test_missing_destination_item_sets_verification_failed(self) -> None:
        """When a destination item is missing upon verification readback, verification is FAILED."""
        job, destination = self._setup_job_with_items(["track-1", "track-2"])

        # Override save_track to only store track-1, simulating a dropped write
        original_save = destination.save_track

        def drop_second_save(tid: str) -> None:
            if tid == "dst-track-2":
                return  # Silently dropped at destination
            original_save(tid)

        destination.save_track = drop_second_save  # type: ignore[method-assign]

        result = self.service.execute(job, destination, confirmed=True)

        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.verification_status, VerificationStatus.FAILED)
        self.assertEqual(result["verification_status"], VerificationStatus.FAILED)
        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.verification_status, VerificationStatus.FAILED)

    def test_playlist_order_mismatch_sets_verification_failed(self) -> None:
        """When playlist items land out of expected order, verification is FAILED."""
        job = self.service.create_job(
            new_account("src-pl"),
            new_account("dst-pl"),
            content=(ContentType.PLAYLISTS,),
        )
        destination = FakePlatformAdapter()

        # Create playlist container item
        container_item = TransferItem.create(
            job.id,
            EntityType.PLAYLIST,
            Platform.TIDAL,
            "pl-1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_PLAYLIST,
        )
        container_item.source_metadata = {"name": "Test Playlist"}
        container_item.mark(ItemStatus.MATCHED)

        # Create two playlist items: write_position 0 and 1
        item_a = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "track-a",
            Platform.TIDAL,
            original_position=0,
            write_position=0,
            container_source_id="pl-1",
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_a.destination_id = "dst-track-a"
        item_a.mark(ItemStatus.MATCHED)

        item_b = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "track-b",
            Platform.TIDAL,
            original_position=1,
            write_position=1,
            container_source_id="pl-1",
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        item_b.destination_id = "dst-track-b"
        item_b.mark(ItemStatus.MATCHED)

        self.service.items.add_many([container_item, item_a, item_b])
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
            for it in [container_item, item_a, item_b]
        )
        plan = TransferPlan.create(job.id, revision=1, items=plan_items)
        self.service.plans.save(plan)
        job.active_plan_id = plan.plan_id
        job.active_plan_revision = plan.revision
        job.active_plan_hash = plan.plan_hash
        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Make destination playlist return inverted order during verification: [dst-track-b, dst-track-a]
        original_playlist_item_ids = destination.playlist_item_ids

        def inverted_ids(playlist_id: str) -> list[str]:
            ids = original_playlist_item_ids(playlist_id)
            if len(ids) == 2:
                return list(reversed(ids))
            return ids

        destination.playlist_item_ids = inverted_ids  # type: ignore[method-assign]

        result = self.service.execute(job, destination, confirmed=True)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.verification_status, VerificationStatus.FAILED)
        self.assertEqual(result["verification_status"], VerificationStatus.FAILED)

    def test_unexpected_item_sets_verification_failed(self) -> None:
        """When destination contains unexpected items, verification is FAILED."""
        job, destination = self._setup_job_with_items(["track-1"])

        original_state = destination.get_destination_state

        def extra_track_state(sections=None) -> DestinationState:
            state = original_state(sections)
            return DestinationState(
                platform=state.platform,
                track_ids=frozenset(list(state.track_ids) + ["unexpected-dst-track"]),
                complete_sections=state.complete_sections,
            )


        destination.get_destination_state = extra_track_state  # type: ignore[method-assign]

        result = self.service.execute(job, destination, confirmed=True)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.verification_status, VerificationStatus.FAILED)
        self.assertEqual(result["verification_status"], VerificationStatus.FAILED)

    def test_unsupported_verification_is_not_passed(self) -> None:
        """When destination raises UnsupportedCapabilityError during readback, verification is PARTIAL."""
        job, destination = self._setup_job_with_items(["track-1"])

        def unsupported_state(sections=None) -> DestinationState:
            raise UnsupportedCapabilityError("read_liked_tracks")

        destination.get_destination_state = unsupported_state  # type: ignore[method-assign]

        result = self.service.execute(job, destination, confirmed=True)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.verification_status, VerificationStatus.PARTIAL)
        self.assertNotEqual(job.verification_status, VerificationStatus.PASSED)
        self.assertEqual(result["verification_status"], VerificationStatus.PARTIAL)

    def test_incomplete_destination_state_is_not_passed(self) -> None:
        """When destination state is marked incomplete (untrustworthy), verification is PARTIAL."""
        job, destination = self._setup_job_with_items(["track-1"])

        def incomplete_state(sections=None) -> DestinationState:
            return DestinationState(
                platform=Platform.TIDAL,
                track_ids=frozenset(["dst-track-1"]),
                incomplete_sections=("tracks",),
            )

        destination.get_destination_state = incomplete_state  # type: ignore[method-assign]

        result = self.service.execute(job, destination, confirmed=True)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.verification_status, VerificationStatus.PARTIAL)
        self.assertNotEqual(job.verification_status, VerificationStatus.PASSED)
        self.assertEqual(result["verification_status"], VerificationStatus.PARTIAL)

    def test_verification_failure_does_not_change_transferred_item_status(self) -> None:
        """Verification failure never rewrites historical TRANSFERRED item status (Invariant H)."""
        job, destination = self._setup_job_with_items(["track-1", "track-2"])

        original_save = destination.save_track

        def drop_second(tid: str) -> None:
            if tid == "dst-track-2":
                return
            original_save(tid)

        destination.save_track = drop_second  # type: ignore[method-assign]

        self.service.execute(job, destination, confirmed=True)
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.verification_status, VerificationStatus.FAILED)

        # Confirm items retain their execution statuses
        items = self.service.items.list_for_job(job.id)
        for item in items:
            self.assertEqual(item.status, ItemStatus.TRANSFERRED)

    # -- Section 28: Fatal verification error handling ------------------------

    def test_authentication_error_during_verification_marks_job_failed(self) -> None:
        """AuthenticationError during verification read transitions job to FAILED / NOT_RUN."""
        job, destination = self._setup_job_with_items(["track-1"])

        def auth_error_state(sections=None) -> DestinationState:
            raise AuthenticationError(message="token_expired_at_verification")

        destination.get_destination_state = auth_error_state  # type: ignore[method-assign]

        result = self.service.execute(job, destination, confirmed=True)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.verification_status, VerificationStatus.NOT_RUN)
        self.assertEqual(result["verification_status"], VerificationStatus.NOT_RUN)
        self.assertEqual(job.error_code, "authentication_error")
        self.assertIsNotNone(job.finished_at)

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.FAILED)
        self.assertEqual(reloaded.error_code, "authentication_error")
        self.assertEqual(reloaded.verification_status, VerificationStatus.NOT_RUN)

    def test_authorization_error_during_verification_marks_job_failed(self) -> None:
        """AuthorizationError during verification read transitions job to FAILED / NOT_RUN."""
        job, destination = self._setup_job_with_items(["track-1"])

        def authz_error_state(sections=None) -> DestinationState:
            raise AuthorizationError(message="read_scope_missing")

        destination.get_destination_state = authz_error_state  # type: ignore[method-assign]

        result = self.service.execute(job, destination, confirmed=True)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.verification_status, VerificationStatus.NOT_RUN)
        self.assertEqual(result["verification_status"], VerificationStatus.NOT_RUN)
        self.assertEqual(job.error_code, "authorization_error")
        self.assertIsNotNone(job.finished_at)

    def test_unexpected_error_during_verification_marks_job_failed(self) -> None:
        """Unexpected runtime exception during verification read marks job FAILED / NOT_RUN."""
        job, destination = self._setup_job_with_items(["track-1"])

        def crash_state(sections=None) -> DestinationState:
            raise RuntimeError("unexpected_platform_crash")

        destination.get_destination_state = crash_state  # type: ignore[method-assign]

        result = self.service.execute(job, destination, confirmed=True)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, "verification_error")
        self.assertEqual(job.verification_status, VerificationStatus.NOT_RUN)
        self.assertEqual(result["verification_status"], VerificationStatus.NOT_RUN)
        self.assertIsNotNone(job.finished_at)

    # -- Section 29: Dry run lifecycle ----------------------------------------

    def test_dry_run_completes_without_fake_verification_success(self) -> None:
        """Dry-run execution completes as COMPLETED with NOT_RUN verification status."""
        job = self.service.create_job(
            new_account("src-dry"),
            new_account("dst-dry"),
            settings=TransferSettings(dry_run=True),
        )
        destination = FakePlatformAdapter()
        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-dry",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.destination_id = "dst-track-dry"
        item.mark(ItemStatus.MATCHED)
        self.service.items.add_many([item])
        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)
        confirm_job_items(self.service, job)

        result = self.service.execute(job, destination, confirmed=True)

        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.verification_status, VerificationStatus.NOT_RUN)
        self.assertEqual(result["verification_status"], VerificationStatus.NOT_RUN)
        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.COMPLETED)
        self.assertEqual(reloaded.verification_status, VerificationStatus.NOT_RUN)

    test_dry_run_remains_completed_with_verification_not_run = (
        test_dry_run_completes_without_fake_verification_success
    )

    # -- Section 30: Finalization Matrix --------------------------------------

    def test_finalization_matrix_cancel(self) -> None:
        """Cancellation produces CANCELLED + NOT_RUN."""
        job, destination = self._setup_job_with_items(["track-1", "track-2"])
        token = CancellationToken()
        token.cancel()

        result = self.service.execute(job, destination, confirmed=True, cancellation_token=token)
        self.assertEqual(job.status, JobStatus.CANCELLED)
        self.assertEqual(job.verification_status, VerificationStatus.NOT_RUN)
        self.assertEqual(result["verification_status"], VerificationStatus.NOT_RUN)

    def test_finalization_matrix_fatal_abort(self) -> None:
        """Fatal execution abort produces FAILED + NOT_RUN."""
        job = self.service.create_job(new_account("src-ab"), new_account("dst-ab"))
        destination = FakePlatformAdapter(
            fail_on={"save_track"},
            error_factory=lambda: AuthenticationError("token_revoked"),
        )
        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "t1",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.destination_id = "dst-t1"
        item.mark(ItemStatus.MATCHED)
        self.service.items.add_many([item])
        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)
        confirm_job_items(self.service, job)

        result = self.service.execute(job, destination, confirmed=True)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.verification_status, VerificationStatus.NOT_RUN)
        self.assertEqual(result["verification_status"], VerificationStatus.NOT_RUN)

    # -- Section 31: Backward compatibility -----------------------------------

    def test_legacy_job_without_verification_status_defaults_to_not_run(self) -> None:
        """Legacy persisted jobs lacking verification_status load safely as NOT_RUN."""
        legacy_data = {
            "id": "job_legacy_123",
            "source_platform": "tidal",
            "destination_platform": "spotify",
            "status": "completed",
            "total_items": 10,
            "processed_items": 10,
            "successful_items": 10,
            # Note: "verification_status" is intentionally absent
        }
        loaded = TransferJob.from_dict(legacy_data)
        self.assertEqual(loaded.status, JobStatus.COMPLETED)
        self.assertEqual(loaded.verification_status, VerificationStatus.NOT_RUN)

    def test_unknown_persisted_verification_status_fails_closed(self) -> None:
        """Explicit unknown/corrupted verification_status fails closed with InvalidPersistedStateError."""
        data = {
            "id": "job_corrupt_123",
            "source_platform": "tidal",
            "destination_platform": "spotify",
            "status": "completed",
            "total_items": 10,
            "processed_items": 10,
            "successful_items": 10,
            "verification_status": "completely_bogus_status",
        }
        with self.assertRaises(InvalidPersistedStateError):
            TransferJob.from_dict(data)

        # Confirm repository deserialization also raises InvalidPersistedStateError
        with tempfile.TemporaryDirectory() as td:
            repo = JsonTransferJobRepository(Path(td))
            from music_transfer.infrastructure.persistence import atomic_write_json
            atomic_write_json(repo._path("job_corrupt_123"), data)
            with self.assertRaises(InvalidPersistedStateError):
                repo.get("job_corrupt_123")

    # -- Section 13: Result aggregation unit tests ----------------------------

    def test_aggregate_verification_status_rules(self) -> None:
        """Unit tests for aggregate_verification_status logic."""
        # Empty when verification not attempted -> NOT_RUN
        self.assertEqual(
            aggregate_verification_status({}, verification_attempted=False),
            VerificationStatus.NOT_RUN,
        )

        # Empty when verification attempted -> PARTIAL (never PASSED or NOT_RUN)
        self.assertEqual(
            aggregate_verification_status({}, verification_attempted=True),
            VerificationStatus.PARTIAL,
        )

        # All passed -> PASSED
        passed_result = VerificationResult(success=True, expected_count=5, actual_count=5)
        self.assertEqual(
            aggregate_verification_status({"tracks": passed_result}),
            VerificationStatus.PASSED,
        )

        # Missing item -> FAILED
        missing_result = VerificationResult(
            success=False,
            expected_count=2,
            actual_count=1,
            missing=["t2"],
        )
        self.assertEqual(
            aggregate_verification_status({"tracks": missing_result}),
            VerificationStatus.FAILED,
        )

        # Order mismatch -> FAILED
        order_result = VerificationResult(
            success=False,
            expected_count=2,
            actual_count=2,
            order_mismatches=[{"position": 0, "expected": "a", "actual": "b"}],
        )
        self.assertEqual(
            aggregate_verification_status({"playlist:p1": order_result}),
            VerificationStatus.FAILED,
        )

        # Unexpected item -> FAILED
        unexpected_result = VerificationResult(
            success=False,
            expected_count=1,
            actual_count=2,
            unexpected=["extra"],
        )
        self.assertEqual(
            aggregate_verification_status({"tracks": unexpected_result}),
            VerificationStatus.FAILED,
        )

        # Unsupported capability -> PARTIAL
        unsupported_result = VerificationResult(
            success=False,
            expected_count=5,
            warnings=["verification_unsupported:read_liked_tracks"],
        )
        self.assertEqual(
            aggregate_verification_status({"tracks": unsupported_result}),
            VerificationStatus.PARTIAL,
        )

        # Incomplete destination state -> PARTIAL
        incomplete_result = VerificationResult(
            success=False,
            expected_count=5,
            warnings=["destination_state_incomplete"],
        )
        self.assertEqual(
            aggregate_verification_status({"tracks": incomplete_result}),
            VerificationStatus.PARTIAL,
        )

        # One passed + one unsupported -> PARTIAL
        self.assertEqual(
            aggregate_verification_status({
                "tracks": passed_result,
                "albums": unsupported_result,
            }),
            VerificationStatus.PARTIAL,
        )

        # Discrepancy + unsupported -> FAILED (discrepancy takes precedence)
        self.assertEqual(
            aggregate_verification_status({
                "tracks": missing_result,
                "albums": unsupported_result,
            }),
            VerificationStatus.FAILED,
        )

    # -- Section 16: Report and View integration ------------------------------

    def test_report_and_view_expose_verification_status(self) -> None:
        """TransferReport and ReportView expose the explicit verification_status."""
        job, destination = self._setup_job_with_items(["track-1"])
        result = self.service.execute(job, destination, confirmed=True)

        report = result["report"]
        self.assertEqual(report.verification_status, VerificationStatus.PASSED)
        self.assertEqual(report.as_dict()["verification_status"], "passed")

        # Re-derived report from service
        regenerated = self.service.report_for(job)
        self.assertEqual(regenerated.verification_status, VerificationStatus.PASSED)

        # ReportView
        view = report_view(job, report)
        self.assertEqual(view.verification_status, "passed")
        self.assertEqual(view.as_dict()["verification_status"], "passed")


    def test_normal_verification_with_no_results_is_partial_not_not_run(self) -> None:
        """When execution runs normally but verification produces no results, status is COMPLETED / PARTIAL with warning."""
        # Create a job with an item that does not generate verifiable sets/playlists
        # (e.g., entity_type unsupported for verification or only skipped items without destination_id)
        job = self.service.create_job(new_account("src-skip"), new_account("dst-skip"))
        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-skip",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.mark(ItemStatus.SKIPPED)
        self.service.items.add_many([item])
        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)
        confirm_job_items(self.service, job)

        destination = FakePlatformAdapter()
        result = self.service.execute(job, destination, confirmed=True)

        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.verification_status, VerificationStatus.PARTIAL)
        self.assertNotEqual(job.verification_status, VerificationStatus.NOT_RUN)
        self.assertNotEqual(job.verification_status, VerificationStatus.PASSED)
        self.assertEqual(result["verification_status"], VerificationStatus.PARTIAL)
        self.assertIn("nothing_verifiable", result["report"].warnings)

    def test_empty_verification_is_never_passed(self) -> None:
        """Empty verification results must never aggregate to PASSED under any circumstances."""
        self.assertNotEqual(
            aggregate_verification_status({}, verification_attempted=True),
            VerificationStatus.PASSED,
        )
        self.assertNotEqual(
            aggregate_verification_status({}, verification_attempted=False),
            VerificationStatus.PASSED,
        )

    def test_already_existing_track_is_included_in_expected_verification_state(self) -> None:
        """Confirmed desired ALREADY_EXISTS set-like entity is included in expected verification state without mutating item status."""
        job = self.service.create_job(new_account("src-ae"), new_account("dst-ae"))
        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-ae",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.destination_id = "dst-track-ae"
        # Item was found already existing at destination during execution
        item.mark(ItemStatus.ALREADY_EXISTS)
        self.service.items.add_many([item])
        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)
        confirm_job_items(self.service, job)

        # Destination contains the item
        destination = FakePlatformAdapter(
            tracks=[track("Track AE", identifier="dst-track-ae")]
        )

        result = self.service.execute(job, destination, confirmed=True)

        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.verification_status, VerificationStatus.PASSED)
        self.assertEqual(result["verification_status"], VerificationStatus.PASSED)
        self.assertIn("tracks", result["verification"])
        self.assertEqual(result["verification"]["tracks"]["expected_count"], 1)
        self.assertEqual(result["verification"]["tracks"]["actual_count"], 1)

        # Invariant: verification must remain read-only and not mutate ALREADY_EXISTS into TRANSFERRED
        persisted_items = self.service.items.list_for_job(job.id)
        self.assertEqual(len(persisted_items), 1)
        self.assertEqual(persisted_items[0].status, ItemStatus.ALREADY_EXISTS)


if __name__ == "__main__":
    unittest.main()

"""Tests for TransferPlan Identity, Revisioning, and Confirmation Safety (Phase 1.3B)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from music_transfer.app.services import TransferService
from music_transfer.core.domain import (
    PlanPrecondition,
    TransferItem,
    TransferPlan,
    TransferPlanItem,
    TransferSettings,
)
from music_transfer.core.enums import (
    ContentType,
    EntityType,
    ItemStatus,
    JobStatus,
    MatchMethod,
    MutationState,
    OrderingMode,
    Platform,
    PreconditionExpectation,
    TransferOperation,
    VerificationStatus,
)
from music_transfer.core.errors import (
    AuthenticationError,
    ConfirmationRequired,
    InvalidPersistedStateError,
    PlanConfirmationMismatch,
    PlanIntegrityError,
    PlanStaleError,
    PlanValidationUnavailableError,
    TemporaryPlatformError,
    UnsupportedCapabilityError,
)
from music_transfer.core.ports import DestinationState
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
    JsonTransferPlanRepository,
)

from tests.support import FakePlatformAdapter, playlist, snapshot, track


def build_service(root: Path) -> TransferService:
    return TransferService(
        JsonTransferJobRepository(root),
        JsonTransferItemRepository(root),
        plans_repository=JsonTransferPlanRepository(root),
    )


class PlanIdentityAndRevisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = build_service(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_plan_has_id_revision_and_sha256_hash(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)

        self.assertTrue(plan.plan_id.startswith("plan_"))
        self.assertEqual(plan.revision, 1)
        self.assertEqual(len(plan.plan_hash), 64)  # SHA-256 hex digest length
        self.assertTrue(plan.verify_integrity())
        self.assertEqual(job.active_plan_id, plan.plan_id)
        self.assertEqual(job.active_plan_revision, 1)
        self.assertEqual(job.active_plan_hash, plan.plan_hash)

    def test_plan_hash_survives_serialization(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Song A", identifier="sa"), track("Song B", identifier="sb")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        original_hash = plan.plan_hash

        # Load fresh from repository
        loaded = self.service.plans.get_by_id(plan.plan_id)
        assert loaded is not None

        self.assertEqual(loaded.plan_hash, original_hash)
        self.assertEqual(loaded.compute_hash(), original_hash)
        self.assertTrue(loaded.verify_integrity())

    def test_runtime_only_fields_do_not_change_plan_hash(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Song A", identifier="sa")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        initial_hash = plan.plan_hash

        # Mutate runtime items in repository
        items = self.service.items.list_for_job(job.id)
        self.assertEqual(len(items), 1)
        items[0].attempt_count = 5
        items[0].mutation_state = MutationState.IN_FLIGHT
        items[0].status = ItemStatus.TRANSFERRED
        items[0].last_error = "transient_network"
        self.service.items.update(items[0])

        # Plan itself should remain unmodified and hash unchanged
        reloaded_plan = self.service.plans.get_by_id(plan.plan_id)
        assert reloaded_plan is not None
        self.assertEqual(reloaded_plan.plan_hash, initial_hash)
        self.assertEqual(reloaded_plan.compute_hash(), initial_hash)

    def test_second_plan_increments_revision(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan1 = self.service.analyze(job, source, destination)
        self.assertEqual(plan1.revision, 1)
        self.assertEqual(job.active_plan_revision, 1)

        # Re-plan (Section 15, Invariant D)
        plan2 = self.service.analyze(job, source, destination)
        self.assertEqual(plan2.revision, 2)
        self.assertNotEqual(plan2.plan_id, plan1.plan_id)
        self.assertEqual(job.active_plan_revision, 2)
        self.assertEqual(job.active_plan_id, plan2.plan_id)

        # Both revisions exist in repository (Section 8)
        rev1 = self.service.plans.get_revision(job.id, 1)
        rev2 = self.service.plans.get_revision(job.id, 2)
        assert rev1 is not None
        assert rev2 is not None
        self.assertEqual(rev1.plan_id, plan1.plan_id)
        self.assertEqual(rev2.plan_id, plan2.plan_id)

    def test_reordering_plan_items_changes_plan_hash(self) -> None:
        item_a = TransferPlanItem(
            entity_type=EntityType.TRACK,
            source_id="t1",
            destination_id="d1",
            operation=TransferOperation.SAVE_TRACK,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            original_position=0,
        )
        item_b = TransferPlanItem(
            entity_type=EntityType.TRACK,
            source_id="t2",
            destination_id="d2",
            operation=TransferOperation.SAVE_TRACK,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            original_position=1,
        )

        plan_ab = TransferPlan.create("job_1", revision=1, items=(item_a, item_b))
        plan_ba = TransferPlan.create("job_1", revision=1, items=(item_b, item_a))

        self.assertNotEqual(plan_ab.plan_hash, plan_ba.plan_hash)

    def test_none_and_zero_positions_do_not_collide_in_plan_identity(self) -> None:
        item_pos_zero = TransferPlanItem(
            entity_type=EntityType.TRACK,
            source_id="t1",
            destination_id="d1",
            operation=TransferOperation.SAVE_TRACK,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            original_position=0,
        )
        item_pos_none = TransferPlanItem(
            entity_type=EntityType.TRACK,
            source_id="t1",
            destination_id="d1",
            operation=TransferOperation.SAVE_TRACK,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            original_position=None,
        )

        self.assertNotEqual(item_pos_zero.canonical_dict(), item_pos_none.canonical_dict())

        plan_zero = TransferPlan.create("job_1", revision=1, items=(item_pos_zero,))
        plan_none = TransferPlan.create("job_1", revision=1, items=(item_pos_none,))

        self.assertNotEqual(plan_zero.plan_hash, plan_none.plan_hash)

    def test_mutation_driving_playlist_metadata_changes_plan_hash(self) -> None:
        item_a = TransferPlanItem(
            entity_type=EntityType.PLAYLIST,
            source_id="p1",
            destination_id=None,
            operation=TransferOperation.CREATE_PLAYLIST,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            source_metadata={"name": "My Playlist", "description": "Desc A"},
        )
        item_b = TransferPlanItem(
            entity_type=EntityType.PLAYLIST,
            source_id="p1",
            destination_id=None,
            operation=TransferOperation.CREATE_PLAYLIST,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            source_metadata={"name": "My Playlist", "description": "Desc B"},
        )
        item_c = TransferPlanItem(
            entity_type=EntityType.PLAYLIST,
            source_id="p1",
            destination_id=None,
            operation=TransferOperation.CREATE_PLAYLIST,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            source_metadata={"name": "Different Name", "description": "Desc A"},
        )

        plan_a = TransferPlan.create("job_1", revision=1, items=(item_a,))
        plan_b = TransferPlan.create("job_1", revision=1, items=(item_b,))
        plan_c = TransferPlan.create("job_1", revision=1, items=(item_c,))

        self.assertNotEqual(plan_a.plan_hash, plan_b.plan_hash)
        self.assertNotEqual(plan_a.plan_hash, plan_c.plan_hash)

    def test_full_source_metadata_survives_plan_hash_roundtrip(self) -> None:
        metadata = {
            "title": "Song Title",
            "artists": ["Artist 1", "Artist 2"],
            "isrc": "USRC12345678",
            "duration_ms": 240000,
            "name": "Playlist Name",
            "description": "Playlist Description",
            "track_count": 42,
        }
        item = TransferPlanItem(
            entity_type=EntityType.TRACK,
            source_id="t1",
            destination_id="d1",
            operation=TransferOperation.SAVE_TRACK,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            source_metadata=metadata,
        )
        plan = TransferPlan.create("job_1", revision=1, items=(item,))
        original_hash = plan.plan_hash

        payload = plan.as_dict()
        reloaded = TransferPlan.from_dict(payload)
        reloaded.plan_hash = original_hash

        self.assertTrue(reloaded.verify_integrity())
        self.assertEqual(reloaded.compute_hash(), original_hash)
        self.assertEqual(reloaded.items[0].source_metadata, metadata)

    def test_nullable_identifier_values_do_not_canonicalize_to_same_intent(self) -> None:
        item_none = TransferPlanItem(
            entity_type=EntityType.TRACK,
            source_id="t1",
            destination_id=None,
            operation=TransferOperation.NONE,
            planned_status=ItemStatus.PENDING,
            match_method=MatchMethod.NONE,
            match_score=0.0,
        )
        item_empty = TransferPlanItem(
            entity_type=EntityType.TRACK,
            source_id="t1",
            destination_id="",
            operation=TransferOperation.NONE,
            planned_status=ItemStatus.PENDING,
            match_method=MatchMethod.NONE,
            match_score=0.0,
        )

        self.assertNotEqual(item_none.intent_payload(), item_empty.intent_payload())
        self.assertNotEqual(item_none.canonical_dict(), item_empty.canonical_dict())

        plan_none = TransferPlan.create("job_1", revision=1, items=(item_none,))
        plan_empty = TransferPlan.create("job_1", revision=1, items=(item_empty,))
        self.assertNotEqual(plan_none.plan_hash, plan_empty.plan_hash)

        # Test container identifiers as well
        item_c_none = TransferPlanItem(
            entity_type=EntityType.PLAYLIST_ITEM,
            source_id="t1",
            destination_id="d1",
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            container_source_id=None,
            container_destination_id=None,
        )
        item_c_empty = TransferPlanItem(
            entity_type=EntityType.PLAYLIST_ITEM,
            source_id="t1",
            destination_id="d1",
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
            planned_status=ItemStatus.MATCHED,
            match_method=MatchMethod.DIRECT_ID,
            match_score=1.0,
            container_source_id="",
            container_destination_id="",
        )
        self.assertNotEqual(item_c_none.intent_payload(), item_c_empty.intent_payload())
        plan_c_none = TransferPlan.create("job_1", revision=1, items=(item_c_none,))
        plan_c_empty = TransferPlan.create("job_1", revision=1, items=(item_c_empty,))
        self.assertNotEqual(plan_c_none.plan_hash, plan_c_empty.plan_hash)

    def test_invalid_explicit_plan_setting_fails_closed_during_integrity_validation(self) -> None:
        plan = TransferPlan.create("job_1", revision=1, items=())
        plan.metadata["settings"] = {"ordering": "corrupted_invalid_mode"}

        with self.assertRaises(InvalidPersistedStateError):
            plan.canonical_payload()

        with self.assertRaises(InvalidPersistedStateError):
            plan.compute_hash()


class PlanConfirmationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = build_service(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_exact_plan_confirmation_succeeds(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.confirmed_plan_id, plan.plan_id)
        self.assertEqual(reloaded.confirmed_plan_revision, plan.revision)
        self.assertEqual(reloaded.confirmed_plan_hash, plan.plan_hash)
        self.assertIsNotNone(reloaded.confirmed_at)

        # Execution succeeds and writes to destination
        result = self.service.execute(job, destination, confirmed=True)
        self.assertEqual(result["report"].transferred, 1)
        self.assertEqual(destination.saved_tracks, ["t1"])

    def test_wrong_plan_id_is_rejected(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        with self.assertRaises((PlanConfirmationMismatch, PlanIntegrityError)):
            self.service.confirm_plan(
                job,
                plan_id="plan_wrong_id",
                revision=plan.revision,
                plan_hash=plan.plan_hash,
            )

        self.assertIsNone(job.confirmed_plan_id)
        with self.assertRaises(ConfirmationRequired):
            self.service.execute(job, destination, confirmed=True)
        self.assertEqual(destination.write_calls, [])

    def test_wrong_revision_is_rejected(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        with self.assertRaises(PlanConfirmationMismatch):
            self.service.confirm_plan(
                job,
                plan_id=plan.plan_id,
                revision=999,
                plan_hash=plan.plan_hash,
            )

        self.assertIsNone(job.confirmed_plan_id)
        with self.assertRaises(ConfirmationRequired):
            self.service.execute(job, destination, confirmed=True)
        self.assertEqual(destination.write_calls, [])

    def test_wrong_hash_is_rejected(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        with self.assertRaises(PlanConfirmationMismatch):
            self.service.confirm_plan(
                job,
                plan_id=plan.plan_id,
                revision=plan.revision,
                plan_hash="0" * 64,
            )

        self.assertIsNone(job.confirmed_plan_id)
        with self.assertRaises(ConfirmationRequired):
            self.service.execute(job, destination, confirmed=True)
        self.assertEqual(destination.write_calls, [])

    def test_confirmed_true_without_exact_confirmation_cannot_write(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        self.service.analyze(job, source, destination)

        # Attempt to call execute(confirmed=True) without calling confirm_plan
        with self.assertRaises(ConfirmationRequired):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_replan_invalidates_previous_confirmation(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan1 = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan1.plan_id,
            revision=plan1.revision,
            plan_hash=plan1.plan_hash,
        )
        self.assertEqual(job.confirmed_plan_id, plan1.plan_id)

        # Re-plan invalidates previous confirmation (Invariant D)
        self.service.analyze(job, source, destination)
        self.assertIsNone(job.confirmed_plan_id)
        self.assertIsNone(job.confirmed_plan_revision)
        self.assertIsNone(job.confirmed_plan_hash)

        # Executing without confirming plan2 must fail with zero destination writes
        with self.assertRaises(ConfirmationRequired):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])


class PlanIntegrityAndDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = build_service(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_tampered_persisted_plan_is_rejected_before_write(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Tamper with the plan JSON on disk
        plan_file = self.root / "plans" / f"{job.id}_rev{plan.revision}_{plan.plan_id}.json"
        import json
        with open(plan_file) as f:
            data = json.load(f)
        # Modify an item without updating the hash
        data["items"][0]["destination_id"] = "tampered_dst_id"
        with open(plan_file, "w") as f:
            json.dump(data, f)

        # Also tamper the latest pointer
        latest_file = self.root / "plans" / f"{job.id}.json"
        with open(latest_file, "w") as f:
            json.dump(data, f)

        # Execution must fail closed before any writes (Invariant F)
        with self.assertRaises(PlanIntegrityError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_execution_item_intent_drift_is_rejected_before_write(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Modify runtime item destination_id before execution
        items = self.service.items.list_for_job(job.id)
        items[0].destination_id = "drifted_target_id"
        self.service.items.update(items[0])

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_destination_item_became_present_stales_plan(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Destination item becomes present before first write
        destination.tracks.append(track("Track 1", identifier="t1"))

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_destination_item_became_absent_stales_plan(self) -> None:
        # Pre-existing item planned as ALREADY_EXISTS
        existing_track = track("Track 1", identifier="t1")
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[existing_track])
        destination = FakePlatformAdapter(tracks=[existing_track])

        plan = self.service.analyze(job, source, destination)
        self.assertEqual(plan.summary.already_exists_items, 1)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Destination item is deleted before execution
        destination.tracks.clear()

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_incomplete_state_does_not_invent_absent_precondition(self) -> None:
        from music_transfer.core.domain import TransferSettings

        job = self.service.create_job(
            Platform.TIDAL, Platform.TIDAL, settings=TransferSettings(skip_already_existing=False)
        )

        destination = FakePlatformAdapter()

        # Destination state with incomplete/untrustworthy section
        incomplete_state = DestinationState(
            platform=Platform.TIDAL,
            incomplete_sections=("tracks",),
        )

        from music_transfer.core.transfer import TransferPlanner
        planner = TransferPlanner()
        res = planner.build(job, snapshot(tracks=(track("Track 1", identifier="t1"),)), destination, destination_state=incomplete_state)

        # Incomplete section must NOT generate ABSENT preconditions (Invariant J)
        absent_preconditions = [p for p in res.plan.preconditions if p.expected == "absent"]
        self.assertEqual(len(absent_preconditions), 0)


    def test_required_preflight_unavailable_fails_closed(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Make destination throw TemporaryPlatformError during preflight check
        destination.fail_on.add("get_destination_state")
        destination.error_factory = lambda: TemporaryPlatformError("network_timeout")

        with self.assertRaises(PlanValidationUnavailableError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_authentication_error_during_plan_preflight_is_fatal(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        destination.fail_on.add("get_destination_state")
        destination.error_factory = lambda: AuthenticationError("token_expired")

        with self.assertRaises(AuthenticationError):
            self.service.execute(job, destination, confirmed=True)

        reloaded = self.service.jobs.get(job.id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, JobStatus.FAILED)
        self.assertEqual(reloaded.verification_status, VerificationStatus.NOT_RUN)
        self.assertEqual(destination.write_calls, [])

    def test_missing_runtime_item_from_confirmed_plan_is_rejected(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(
            tracks=[track("Track 1", identifier="t1"), track("Track 2", identifier="t2")]
        )
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.assertEqual(len(plan.items), 2)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Remove item 2 from runtime repository (leaving incomplete subset)
        items = self.service.items.list_for_job(job.id)
        self.assertEqual(len(items), 2)
        self.service.items.replace_for_job(job.id, [items[0]])

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_extra_runtime_item_is_rejected(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.assertEqual(len(plan.items), 1)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Inject extra runtime item
        items = self.service.items.list_for_job(job.id)
        extra_item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "t2",
            Platform.TIDAL,
            original_position=1,
            operation=TransferOperation.SAVE_TRACK,
        )
        extra_item.destination_id = "t2"
        extra_item.status = ItemStatus.MATCHED
        self.service.items.replace_for_job(job.id, [items[0], extra_item])

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_runtime_item_reordering_is_rejected_before_write(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(
            tracks=[track("Track 1", identifier="t1"), track("Track 2", identifier="t2")]
        )
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.assertEqual(len(plan.items), 2)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Reorder runtime items in repository [t2, t1]
        items = self.service.items.list_for_job(job.id)
        self.service.items.replace_for_job(job.id, [items[1], items[0]])

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_requested_content_change_stales_confirmed_plan(self) -> None:
        job = self.service.create_job(
            Platform.TIDAL,
            Platform.TIDAL,
            content=(ContentType.LIKED_TRACKS,),
        )
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Mutate requested_content before execution
        job.requested_content = (ContentType.LIKED_TRACKS, ContentType.SAVED_ALBUMS)
        self.service.jobs.update(job)

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_dry_run_change_stales_confirmed_plan(self) -> None:
        job = self.service.create_job(
            Platform.TIDAL,
            Platform.TIDAL,
            settings=TransferSettings(dry_run=True),
        )
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Mutate dry_run from True to False before execution
        job.settings = TransferSettings(dry_run=False)
        self.service.jobs.update(job)

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_ordering_change_stales_confirmed_plan(self) -> None:
        job = self.service.create_job(
            Platform.TIDAL,
            Platform.TIDAL,
            settings=TransferSettings(ordering=OrderingMode.SOURCE_ORDER),
        )
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Mutate ordering before execution
        job.settings = TransferSettings(ordering=OrderingMode.ALPHABETICAL)
        self.service.jobs.update(job)

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_destination_account_change_stales_confirmed_plan(self) -> None:
        job = self.service.create_job(
            Platform.TIDAL,
            Platform.TIDAL,
        )
        job.destination_account_id = "acc_original"
        self.service.jobs.update(job)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Mutate destination_account_id before execution
        job.destination_account_id = "acc_changed"
        self.service.jobs.update(job)

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_precondition_read_unsupported_fails_closed(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.assertTrue(len(plan.preconditions) > 0)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Make destination throw UnsupportedCapabilityError during preflight check
        destination.fail_on.add("get_destination_state")
        destination.error_factory = lambda: UnsupportedCapabilityError("get_destination_state")

        with self.assertRaises(PlanValidationUnavailableError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_current_precondition_section_untrustworthy_fails_closed(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.assertTrue(len(plan.preconditions) > 0)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Destination returns untrustworthy section for precondition check
        destination.get_destination_state = lambda: DestinationState(  # type: ignore[method-assign]
            platform=Platform.TIDAL,
            incomplete_sections=("tracks",),
        )

        with self.assertRaises(PlanValidationUnavailableError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_unknown_precondition_expectation_fails_closed(self) -> None:
        with self.assertRaises(InvalidPersistedStateError):
            PlanPrecondition(
                entity_type=EntityType.TRACK,
                destination_id="t1",
                expected="unknown_future_expectation",  # type: ignore[arg-type]
                section="tracks",
            )

        with self.assertRaises(InvalidPersistedStateError):
            PlanPrecondition.from_dict({
                "entity_type": "track",
                "destination_id": "t1",
                "expected": "unknown_future_expectation",
                "section": "tracks",
            })

    def test_unknown_precondition_section_fails_closed(self) -> None:
        with self.assertRaises(InvalidPersistedStateError):
            PlanPrecondition(
                entity_type=EntityType.TRACK,
                destination_id="t1",
                expected=PreconditionExpectation.PRESENT,
                section="unknown_section",
            )

        with self.assertRaises(InvalidPersistedStateError):
            PlanPrecondition.from_dict({
                "entity_type": "track",
                "destination_id": "t1",
                "expected": "present",
                "section": "unknown_section",
            })

    def test_playlist_name_metadata_drift_is_rejected_before_write(self) -> None:
        src_pl = playlist("Original Name", [track("Track 1", identifier="t1")])
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        source = FakePlatformAdapter(playlists=[src_pl])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Mutate runtime playlist name before execution
        items = self.service.items.list_for_job(job.id)
        for it in items:
            if it.entity_type is EntityType.PLAYLIST:
                it.source_metadata["name"] = "Changed Name"
                self.service.items.update(it)

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.created_playlists, [])

    def test_playlist_description_metadata_drift_is_rejected_before_write(self) -> None:
        src_pl = playlist(
            "My Playlist",
            [track("Track 1", identifier="t1")],
            description="Original Description",
        )
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        source = FakePlatformAdapter(playlists=[src_pl])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Mutate runtime playlist description before execution
        items = self.service.items.list_for_job(job.id)
        for it in items:
            if it.entity_type is EntityType.PLAYLIST:
                it.source_metadata["description"] = "Changed Description"
                self.service.items.update(it)

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.created_playlists, [])



class ResumeAndCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = build_service(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_confirmation_survives_restart(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Simulate service restart by initializing new service with same root
        restarted_service = build_service(self.root)
        reloaded_job = restarted_service.jobs.get(job.id)
        assert reloaded_job is not None
        self.assertEqual(reloaded_job.confirmed_plan_id, plan.plan_id)

        result = restarted_service.execute(reloaded_job, destination, confirmed=True)
        self.assertEqual(result["report"].transferred, 1)
        self.assertEqual(destination.saved_tracks, ["t1"])

    def test_resume_allows_legitimate_runtime_status_changes(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        source = FakePlatformAdapter(tracks=[track("Track 1", identifier="t1"), track("Track 2", identifier="t2")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Interrupt on second item
        orig_save = destination.save_track

        def interrupt_on_second(tid: str) -> None:
            if tid == "t2":
                raise KeyboardInterrupt()
            orig_save(tid)

        destination.save_track = interrupt_on_second  # type: ignore[method-assign]

        with self.assertRaises(KeyboardInterrupt):
            self.service.execute(job, destination, confirmed=True)

        destination.save_track = orig_save  # type: ignore[method-assign]

        # First track is transferred, job is interrupted
        reloaded_job = self.service.jobs.get(job.id)
        assert reloaded_job is not None
        items = self.service.items.list_for_job(job.id)
        self.assertEqual(items[0].status, ItemStatus.TRANSFERRED)
        self.assertEqual(items[1].status, ItemStatus.MATCHED)

        # Resuming must not reject legitimate runtime item changes (Invariant M)
        result = self.service.resume(reloaded_job, destination, confirmed=True)
        self.assertEqual(result["report"].transferred, 2)
        self.assertEqual(destination.saved_tracks, ["t1", "t2"])

    def test_legacy_unversioned_plan_cannot_authorize_new_write(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL)
        destination = FakePlatformAdapter()

        item = TransferItem.create(
            job.id,
            EntityType.TRACK,
            Platform.TIDAL,
            "t1",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.destination_id = "t1"
        item.mark(ItemStatus.MATCHED)
        self.service.items.add_many([item])

        # Create unversioned/legacy plan
        legacy_plan = TransferPlan(
            job_id=job.id,
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            plan_id="",
            revision=0,
            plan_hash="",
        )
        self.service.plans.save(legacy_plan)

        job.status = JobStatus.WAITING_CONFIRMATION
        self.service.jobs.update(job)

        with self.assertRaises(ConfirmationRequired):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertEqual(destination.saved_tracks, [])

    def test_playlist_stale_confirmation_zero_writes(self) -> None:
        src_pl = playlist("Favorites", [track("Track 1", identifier="t1")])
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        source = FakePlatformAdapter(playlists=[src_pl])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Invalidate active plan by re-planning
        self.service.analyze(job, source, destination)

        # Attempt to execute with old confirmation
        with self.assertRaises(ConfirmationRequired):
            self.service.execute(job, destination, confirmed=True)

        # Assert zero playlist creations and zero playlist item additions (Section 30)
        self.assertEqual(destination.created_playlists, [])
        self.assertEqual(destination.write_calls, [])
        create_calls = [c for c in destination.write_calls if c[0] == "create_playlist"]
        add_item_calls = [c for c in destination.write_calls if c[0] == "add_playlist_item"]
        self.assertEqual(len(create_calls), 0)
        self.assertEqual(len(add_item_calls), 0)


if __name__ == "__main__":
    unittest.main()

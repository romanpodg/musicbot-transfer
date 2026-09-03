"""Regression tests for Phase 1.5C.1: Folder Resume Safety and Container Hardening."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from music_transfer.app.services.transfer_service import TransferService
from music_transfer.core.domain import (
    LibraryRecord,
    LibrarySnapshot,
    TransferItem,
    TransferJob,
    TransferPlan,
    TransferPlanItem,
)
from music_transfer.core.enums import (
    ContentType,
    EntityType,
    ItemStatus,
    MatchMethod,
    MutationState,
    Platform,
    TransferOperation,
)
from music_transfer.core.errors import (
    AuthenticationError,
    PlanStaleError,
    TemporaryPlatformError,
    TransferConfigurationError,
)
from music_transfer.core.ports import ReadOnlyAdapter
from music_transfer.core.transfer import (
    TransferPlanner,
    TransferVerifier,
    validate_folder_hierarchy,
)
from music_transfer.core.transfer.executor import TransferExecutor
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
)

from tests.support import (
    FakePlatformAdapter,
    playlist,
    snapshot,
)


def _make_folder_rec(source_id: str, title: str, parent_id: str | None = None) -> LibraryRecord:
    return LibraryRecord(
        source_platform=Platform.TIDAL,
        source_id=source_id,
        title=title,
        metadata={"parent_source_id": parent_id, "parent_id": parent_id},
    )


class FolderResumeSafetyTests(unittest.TestCase):
    """Section 3-13: No-blind-replay and reconciliation safety across restart/crash."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.job_repo = JsonTransferJobRepository(Path(self.tmp_dir))
        self.item_repo = JsonTransferItemRepository(Path(self.tmp_dir))
        self.service = TransferService(self.job_repo, self.item_repo)

    def test_persisted_inflight_folder_zero_match_never_replays_create(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        item = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder 1"},
        )
        item.destination_id = None
        item.mutation_state = MutationState.IN_FLIGHT
        item.status = ItemStatus.PENDING
        self.item_repo.add_many([item])

        dest = FakePlatformAdapter()
        dest.folders = []  # zero matches

        executor = TransferExecutor(dest, self.item_repo)
        executor.execute(job, [item])

        create_folder_calls = [c for c in dest.write_calls if c[0] == "create_folder"]
        self.assertEqual(len(create_folder_calls), 0)

        persisted = self.item_repo.load(job.id)[0]
        self.assertEqual(persisted.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(persisted.last_failure_kind, "ambiguous")
        self.assertIsNone(persisted.destination_id)

    def test_persisted_inflight_folder_duplicate_matches_never_replay_create(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        item = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder 1"},
        )
        item.destination_id = None
        item.mutation_state = MutationState.IN_FLIGHT
        item.status = ItemStatus.PENDING
        self.item_repo.add_many([item])

        dest = FakePlatformAdapter()
        dest.folders = [
            _make_folder_rec("dst-fld-1", "Folder 1", parent_id=None),
            _make_folder_rec("dst-fld-2", "Folder 1", parent_id=None),
        ]

        executor = TransferExecutor(dest, self.item_repo)
        executor.execute(job, [item])

        create_folder_calls = [c for c in dest.write_calls if c[0] == "create_folder"]
        self.assertEqual(len(create_folder_calls), 0)

        persisted = self.item_repo.load(job.id)[0]
        self.assertEqual(persisted.status, ItemStatus.AMBIGUOUS)
        self.assertIsNone(persisted.destination_id)

    def test_persisted_inflight_folder_incomplete_readback_never_replays_create(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        item = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder 1"},
        )
        item.destination_id = None
        item.mutation_state = MutationState.IN_FLIGHT
        item.status = ItemStatus.PENDING
        self.item_repo.add_many([item])

        dest = FakePlatformAdapter()
        orig_export = dest.export_library

        def export_with_incomplete_folders(sections=None, progress=None) -> LibrarySnapshot:
            snap = orig_export(sections=sections, progress=progress)
            return LibrarySnapshot(
                account=snap.account,
                platform=snap.platform,
                folders=snap.folders,
                incomplete_sections={"folders"},
            )

        dest.export_library = export_with_incomplete_folders  # type: ignore[method-assign]

        executor = TransferExecutor(dest, self.item_repo)
        executor.execute(job, [item])

        create_folder_calls = [c for c in dest.write_calls if c[0] == "create_folder"]
        self.assertEqual(len(create_folder_calls), 0)

        persisted = self.item_repo.load(job.id)[0]
        self.assertEqual(persisted.status, ItemStatus.AMBIGUOUS)
        self.assertIsNone(persisted.destination_id)

    def test_persisted_inflight_folder_readback_error_never_replays_create(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        item = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder 1"},
        )
        item.destination_id = None
        item.mutation_state = MutationState.IN_FLIGHT
        item.status = ItemStatus.PENDING
        self.item_repo.add_many([item])

        dest = FakePlatformAdapter()

        def fail_export(sections=None, progress=None) -> LibrarySnapshot:
            raise TemporaryPlatformError("network_readback_failure")

        dest.export_library = fail_export  # type: ignore[method-assign]

        executor = TransferExecutor(dest, self.item_repo)
        executor.execute(job, [item])

        create_folder_calls = [c for c in dest.write_calls if c[0] == "create_folder"]
        self.assertEqual(len(create_folder_calls), 0)

        persisted = self.item_repo.load(job.id)[0]
        self.assertEqual(persisted.status, ItemStatus.AMBIGUOUS)
        self.assertIsNone(persisted.destination_id)

    def test_persisted_inflight_folder_unique_match_recovers_without_create(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        item = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder 1"},
        )
        item.destination_id = None
        item.mutation_state = MutationState.IN_FLIGHT
        item.status = ItemStatus.PENDING
        self.item_repo.add_many([item])

        dest = FakePlatformAdapter()
        dest.folders = [_make_folder_rec("dst-fld-1", "Folder 1", parent_id=None)]

        executor = TransferExecutor(dest, self.item_repo)
        executor.execute(job, [item])

        create_folder_calls = [c for c in dest.write_calls if c[0] == "create_folder"]
        self.assertEqual(len(create_folder_calls), 0)

        persisted = self.item_repo.load(job.id)[0]
        self.assertEqual(persisted.status, ItemStatus.TRANSFERRED)
        self.assertEqual(persisted.destination_id, "dst-fld-1")
        self.assertEqual(persisted.mutation_state, MutationState.NONE)

    def test_second_resume_of_ambiguous_folder_still_does_not_create(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        item = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder 1"},
        )
        item.destination_id = None
        item.mutation_state = MutationState.IN_FLIGHT
        item.status = ItemStatus.PENDING
        self.item_repo.add_many([item])

        dest = FakePlatformAdapter()
        dest.folders = []

        # First run: reconciles to AMBIGUOUS with 0 creates
        executor1 = TransferExecutor(dest, self.item_repo)
        executor1.execute(job, [item])
        self.assertEqual(len(dest.write_calls), 0)
        self.assertEqual(item.status, ItemStatus.AMBIGUOUS)

        # Second run: item is already AMBIGUOUS, must not execute or create
        executor2 = TransferExecutor(dest, self.item_repo)
        persisted_items = self.item_repo.load(job.id)
        outcome = executor2.execute(job, persisted_items)
        self.assertEqual(len(dest.write_calls), 0)
        self.assertEqual(outcome.skipped, 1)

    def test_folder_reconciliation_auth_error_aborts_execution(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        item = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder 1"},
        )
        item.destination_id = None
        item.mutation_state = MutationState.IN_FLIGHT
        item.status = ItemStatus.PENDING
        self.item_repo.add_many([item])

        dest = FakePlatformAdapter()

        def auth_fail_export(sections=None, progress=None) -> LibrarySnapshot:
            raise AuthenticationError("token_expired")

        dest.export_library = auth_fail_export  # type: ignore[method-assign]

        executor = TransferExecutor(dest, self.item_repo)
        outcome = executor.execute(job, [item])

        self.assertTrue(outcome.aborted)
        self.assertEqual(outcome.abort_error, "token_expired")
        create_folder_calls = [c for c in dest.write_calls if c[0] == "create_folder"]
        self.assertEqual(len(create_folder_calls), 0)


class ParentStateHardeningTests(unittest.TestCase):
    """Section 14-16: Persisted parent failure/ambiguity blocking across restarts."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.job_repo = JsonTransferJobRepository(Path(self.tmp_dir))
        self.item_repo = JsonTransferItemRepository(Path(self.tmp_dir))
        self.service = TransferService(self.job_repo, self.item_repo)

    def test_ambiguous_parent_with_stale_destination_id_blocks_child_folder(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        folder_a = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f_a",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder A"},
        )
        folder_a.status = ItemStatus.AMBIGUOUS
        folder_a.destination_id = "dst-A"  # Stale / contradictory persisted ID!

        folder_b = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f_b",
            Platform.TIDAL,
            container_source_id="f_a",
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder B"},
        )
        folder_b.status = ItemStatus.PENDING

        self.item_repo.add_many([folder_a, folder_b])
        dest = FakePlatformAdapter()

        # Restart with fresh executor
        executor = TransferExecutor(dest, self.item_repo)
        executor.execute(job, [folder_a, folder_b])

        create_folder_calls = [c for c in dest.write_calls if c[0] == "create_folder"]
        self.assertEqual(len(create_folder_calls), 0)

        persisted_b = next(it for it in self.item_repo.load(job.id) if it.source_id == "f_b")
        self.assertEqual(persisted_b.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(persisted_b.last_error, "container_blocked")

    def test_failed_parent_with_stale_destination_id_blocks_playlist(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        folder_a = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f_a",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder A"},
        )
        folder_a.status = ItemStatus.FAILED
        folder_a.destination_id = "dst-A"  # Stale destination ID on failed parent

        playlist_p = TransferItem.create(
            job.id,
            EntityType.PLAYLIST,
            Platform.TIDAL,
            "p_1",
            Platform.TIDAL,
            container_source_id="f_a",
            operation=TransferOperation.CREATE_PLAYLIST,
            source_metadata={"name": "Playlist 1"},
        )
        playlist_p.status = ItemStatus.PENDING

        self.item_repo.add_many([folder_a, playlist_p])
        dest = FakePlatformAdapter()

        executor = TransferExecutor(dest, self.item_repo)
        executor.execute(job, [folder_a, playlist_p])

        create_playlist_calls = [c for c in dest.write_calls if c[0] == "create_playlist"]
        self.assertEqual(len(create_playlist_calls), 0)

        persisted_p = next(it for it in self.item_repo.load(job.id) if it.source_id == "p_1")
        self.assertEqual(persisted_p.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(persisted_p.last_error, "container_blocked")

    def test_restart_preserves_parent_failure_blocking(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        root_fld = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f_root",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Root"},
        )
        root_fld.status = ItemStatus.FAILED

        child_fld = TransferItem.create(
            job.id,
            EntityType.FOLDER,
            Platform.TIDAL,
            "f_child",
            Platform.TIDAL,
            container_source_id="f_root",
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Child"},
        )
        child_fld.status = ItemStatus.PENDING

        pl_item = TransferItem.create(
            job.id,
            EntityType.PLAYLIST,
            Platform.TIDAL,
            "p_1",
            Platform.TIDAL,
            container_source_id="f_child",
            operation=TransferOperation.CREATE_PLAYLIST,
            source_metadata={"name": "Playlist"},
        )
        pl_item.status = ItemStatus.PENDING

        track_item = TransferItem.create(
            job.id,
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "t_1",
            Platform.TIDAL,
            container_source_id="p_1",
            write_position=0,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        track_item.destination_id = "dst-t1"
        track_item.status = ItemStatus.PENDING

        self.item_repo.add_many([root_fld, child_fld, pl_item, track_item])
        dest = FakePlatformAdapter()

        executor = TransferExecutor(dest, self.item_repo)
        executor.execute(job, [root_fld, child_fld, pl_item, track_item])

        self.assertEqual(len(dest.write_calls), 0)

        persisted = {it.source_id: it for it in self.item_repo.load(job.id)}
        self.assertEqual(persisted["f_child"].status, ItemStatus.AMBIGUOUS)
        self.assertEqual(persisted["f_child"].last_error, "container_blocked")
        self.assertEqual(persisted["p_1"].status, ItemStatus.AMBIGUOUS)
        self.assertEqual(persisted["p_1"].last_error, "container_blocked")
        self.assertEqual(persisted["t_1"].status, ItemStatus.AMBIGUOUS)
        self.assertEqual(persisted["t_1"].last_error, "playlist_sequence_blocked")


class FolderMetadataAndRootNormalizationTests(unittest.TestCase):
    """Section 17-24: Strict folder name validation, is_executable, plan hash, and universal root None."""

    def test_folder_missing_name_fails_before_confirmation(self) -> None:
        f = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="f1",
            title="",
        )
        with self.assertRaises(TransferConfigurationError) as ctx:
            validate_folder_hierarchy([f], [])
        self.assertIn("missing_folder_name", str(ctx.exception))

    def test_folder_whitespace_name_fails_before_confirmation(self) -> None:
        f = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="f1",
            title="   \t \n ",
        )
        with self.assertRaises(TransferConfigurationError) as ctx:
            validate_folder_hierarchy([f], [])
        self.assertIn("missing_folder_name", str(ctx.exception))

    def test_create_folder_is_not_executable_from_source_id_alone(self) -> None:
        # No metadata
        item_no_meta = TransferItem.create(
            "j1",
            EntityType.FOLDER,
            Platform.TIDAL,
            "f1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={},
        )
        self.assertFalse(item_no_meta.is_executable())

        # Empty name
        item_empty_name = TransferItem.create(
            "j1",
            EntityType.FOLDER,
            Platform.TIDAL,
            "f1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": ""},
        )
        self.assertFalse(item_empty_name.is_executable())

        # Whitespace name
        item_ws_name = TransferItem.create(
            "j1",
            EntityType.FOLDER,
            Platform.TIDAL,
            "f1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "   \n "},
        )
        self.assertFalse(item_ws_name.is_executable())

        # Valid confirmed name
        item_valid = TransferItem.create(
            "j1",
            EntityType.FOLDER,
            Platform.TIDAL,
            "f1",
            Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "My Folder"},
        )
        self.assertTrue(item_valid.is_executable())

        # Already landed (destination_id present) is not executable
        item_valid.destination_id = "dst-f1"
        self.assertFalse(item_valid.is_executable())

    def test_core_does_not_treat_identifier_named_root_as_root_sentinel(self) -> None:
        # Folder with literal ID "root" (e.g. from provider where "root" is an ordinary identifier)
        f_root_id = _make_folder_rec("root", "Folder With ID Root", parent_id=None)
        f_child = _make_folder_rec("f_child", "Child", parent_id="root")
        p_child = playlist("Pl Under Root ID", [], identifier="p_root_child", folder_id="root")

        # Validation succeeds because parent_id "root" refers to f_root_id, NOT to None!
        sorted_folders = validate_folder_hierarchy([f_root_id, f_child], [p_child])
        self.assertEqual([f.source_id for f in sorted_folders], ["root", "f_child"])

        planner = TransferPlanner()
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.PLAYLISTS,))
        snap = snapshot(
            folders=(f_root_id, f_child),
            playlists=(p_child,),
        )
        dest = FakePlatformAdapter()
        plan_result = planner.build(job, snap, ReadOnlyAdapter(dest))
        items_by_src = {it.source_id: it for it in plan_result.items}

        # Root folder item has container_source_id None
        self.assertIsNone(items_by_src["root"].container_source_id)
        # Child folder item has container_source_id "root"
        self.assertEqual(items_by_src["f_child"].container_source_id, "root")
        # Playlist item has container_source_id "root" (NOT None!)
        self.assertEqual(items_by_src["p_root_child"].container_source_id, "root")

        # Verifier test: destination folder actually has destination ID "root"
        dest.folders = [
            _make_folder_rec("root", "Folder With ID Root", parent_id=None),
        ]
        dest.playlists = [
            playlist("Pl Under Root ID", [], identifier="dst_p1", folder_id="root"),
        ]

        fld_item = TransferItem.create(
            job.id, EntityType.FOLDER, Platform.TIDAL, "root", Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder With ID Root"},
        )
        fld_item.destination_id = "root"
        fld_item.status = ItemStatus.TRANSFERRED

        pl_item = TransferItem.create(
            job.id, EntityType.PLAYLIST, Platform.TIDAL, "p_root_child", Platform.TIDAL,
            container_source_id="root",
            operation=TransferOperation.CREATE_PLAYLIST,
            source_metadata={"name": "Pl Under Root ID"},
        )
        pl_item.destination_id = "dst_p1"
        pl_item.container_destination_id = "root"
        pl_item.status = ItemStatus.TRANSFERRED

        verifier = TransferVerifier(dest)
        results = verifier.verify_job(job, [fld_item, pl_item])
        self.assertTrue(results["folder:root"].success)
        self.assertTrue(results["playlist_placement:dst_p1"].success)

    def test_changing_folder_name_after_confirmation_invalidates_plan_hash(self) -> None:
        tmp_dir = tempfile.mkdtemp()
        job_repo = JsonTransferJobRepository(Path(tmp_dir))
        item_repo = JsonTransferItemRepository(Path(tmp_dir))
        service = TransferService(job_repo, item_repo)

        job = service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        fld = _make_folder_rec("f1", "Original Name", parent_id=None)
        src = FakePlatformAdapter(display_name="source", folders=[fld], playlists=[])
        dest = FakePlatformAdapter(display_name="destination")

        service.analyze(job, src, dest)
        service.confirm_plan(
            job,
            plan_id=job.active_plan_id,
            revision=job.active_plan_revision,
            plan_hash=job.active_plan_hash,
        )

        # Build two plan items with different folder names
        item1 = TransferPlanItem(
            entity_type=EntityType.FOLDER,
            source_id="f1",
            destination_id=None,
            operation=TransferOperation.CREATE_FOLDER,
            planned_status=ItemStatus.PENDING,
            match_method=MatchMethod.NONE,
            match_score=0.0,
            source_metadata={"name": "Original Name"},
        )
        plan1 = TransferPlan(
            job_id=job.id,
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            items=(item1,),
        )

        item2 = TransferPlanItem(
            entity_type=EntityType.FOLDER,
            source_id="f1",
            destination_id=None,
            operation=TransferOperation.CREATE_FOLDER,
            planned_status=ItemStatus.PENDING,
            match_method=MatchMethod.NONE,
            match_score=0.0,
            source_metadata={"name": "Changed Name"},
        )
        plan2 = TransferPlan(
            job_id=job.id,
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            items=(item2,),
        )

        self.assertNotEqual(plan1.compute_hash(), plan2.compute_hash())

        # If confirmed plan hash does not match current plan hash, execution fails closed
        job.confirmed_plan_hash = "stale_hash_mismatch"
        job_repo.update(job)
        with self.assertRaises(PlanStaleError):
            service.execute(job, dest, confirmed=True)
        self.assertEqual(len(dest.write_calls), 0)

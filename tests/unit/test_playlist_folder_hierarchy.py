"""Tests for playlist folder hierarchy transfer and runtime container binding (Phase 1.5C)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from music_transfer.app.services.transfer_service import (
    TransferService,
    content_sections,
    source_export_sections,
)
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
    VerificationStatus,
)
from music_transfer.core.errors import (
    AmbiguousOperationError,
    PermanentPlatformError,
    TemporaryPlatformError,
    TransferConfigurationError,
    UnsupportedCapabilityError,
)
from music_transfer.core.ports import (
    PlatformCapabilities,
    ReadOnlyAdapter,
)
from music_transfer.core.transfer import (
    TransferPlanner,
    TransferVerifier,
    aggregate_verification_status,
    folder_parent_source_id,
    require_transfer_content_spec,
    validate_folder_hierarchy,
)
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
)
from music_transfer.platforms.tidal.adapter import TidalAdapter
from music_transfer.platforms.tidal.errors import TidalClientError
from music_transfer.platforms.tidal.mapper import (
    folder_record_from_tidal,
    playlist_from_tidal,
)

from tests.support import (
    FakePlatformAdapter,
    playlist,
    snapshot,
    track,
)


def _make_folder_rec(source_id: str, title: str, parent_id: str | None = None) -> LibraryRecord:
    return LibraryRecord(
        source_platform=Platform.TIDAL,
        source_id=source_id,
        title=title,
        metadata={"parent_source_id": parent_id, "parent_id": parent_id},
    )


class StructuralContentTests(unittest.TestCase):
    """Section 41: Folders are structural playlist content, not a ContentType."""

    def test_folders_are_structural_playlist_content_not_content_type(self) -> None:
        self.assertEqual(EntityType.FOLDER.value, "folder")
        with self.assertRaises(AttributeError):
            _ = ContentType.FOLDERS  # type: ignore[attr-defined]

        spec = require_transfer_content_spec(ContentType.PLAYLISTS)
        self.assertEqual(len(spec.structural_dependencies), 1)
        dep = spec.structural_dependencies[0]
        self.assertEqual(dep.snapshot_section, "folders")
        self.assertEqual(dep.source_read_capability, "read_folders")
        self.assertEqual(dep.destination_write_capability, "create_folders")

    def test_playlist_primary_presence_sections_remain_playlists_only(self) -> None:
        self.assertEqual(content_sections((ContentType.PLAYLISTS,)), ("playlists",))

    def test_playlist_source_export_includes_folders_when_supported(self) -> None:
        caps = PlatformCapabilities(platform=Platform.TIDAL, read_playlists=True, read_folders=True)
        sections = source_export_sections((ContentType.PLAYLISTS,), caps)
        self.assertEqual(sections, ("folders", "playlists"))

    def test_playlist_source_export_excludes_folders_when_not_supported(self) -> None:
        caps = PlatformCapabilities(platform=Platform.TIDAL, read_playlists=True, read_folders=False)
        sections = source_export_sections((ContentType.PLAYLISTS,), caps)
        self.assertEqual(sections, ("playlists",))

    def test_folder_export_precedes_playlist_export(self) -> None:
        caps = PlatformCapabilities(platform=Platform.TIDAL, read_playlists=True, read_folders=True)
        sections = source_export_sections((ContentType.PLAYLISTS,), caps)
        f_idx = sections.index("folders")
        p_idx = sections.index("playlists")
        self.assertLess(f_idx, p_idx)

    def test_playlist_folder_export_does_not_expand_unrelated_sections(self) -> None:
        caps = PlatformCapabilities(
            platform=Platform.TIDAL,
            read_liked_tracks=True,
            read_saved_albums=True,
            read_followed_artists=True,
            read_videos=True,
            read_mixes=True,
            read_playlists=True,
            read_folders=True,
        )
        sections = source_export_sections((ContentType.PLAYLISTS,), caps)
        self.assertEqual(sections, ("folders", "playlists"))

    def test_flat_playlists_do_not_require_destination_create_folders(self) -> None:
        planner = TransferPlanner()
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.PLAYLISTS,))
        snap = snapshot(
            playlists=(playlist("Flat", [track("T1", identifier="t1")]),),
            folders=(),
        )
        caps = PlatformCapabilities(
            platform=Platform.TIDAL,
            create_playlists=True,
            write_playlist_items=True,
            create_folders=False,  # Destination cannot create folders
        )
        dst = FakePlatformAdapter(capabilities=caps)
        result = planner.build(job, snap, ReadOnlyAdapter(dst))
        self.assertIsNotNone(result.plan)

    def test_nonempty_folder_hierarchy_requires_destination_create_folders(self) -> None:
        planner = TransferPlanner()
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.PLAYLISTS,))
        snap = snapshot(
            playlists=(playlist("Pl1", [track("T1", identifier="t1")]),),
            folders=(_make_folder_rec("f1", "Folder 1"),),
        )
        caps = PlatformCapabilities(
            platform=Platform.TIDAL,
            create_playlists=True,
            write_playlist_items=True,
            create_folders=False,  # Missing create_folders capability!
        )
        dst = FakePlatformAdapter(capabilities=caps)
        with self.assertRaises(UnsupportedCapabilityError):
            planner.build(job, snap, ReadOnlyAdapter(dst))


class FolderValidationTests(unittest.TestCase):
    """Section 43: Folder graph validation and normalization."""

    def test_folder_missing_source_id_raises_error(self) -> None:
        f = LibraryRecord(
            source_platform=Platform.TIDAL,
            source_id="",
            title="Empty ID",
        )
        with self.assertRaises(TransferConfigurationError) as ctx:
            validate_folder_hierarchy([f], [])
        self.assertIn("missing_folder_id", str(ctx.exception))

    def test_folder_duplicate_source_id_raises_error(self) -> None:
        f1 = _make_folder_rec("f1", "Folder 1")
        f2 = _make_folder_rec("f1", "Folder 1 Duplicate")
        with self.assertRaises(TransferConfigurationError) as ctx:
            validate_folder_hierarchy([f1, f2], [])
        self.assertIn("duplicate_folder_id", str(ctx.exception))

    def test_folder_referencing_nonexistent_parent_raises_error(self) -> None:
        f1 = _make_folder_rec("f1", "Child", parent_id="nonexistent")
        with self.assertRaises(TransferConfigurationError) as ctx:
            validate_folder_hierarchy([f1], [])
        self.assertIn("missing_parent", str(ctx.exception))

    def test_folder_self_parenting_raises_error(self) -> None:
        f1 = _make_folder_rec("f1", "Self Parent", parent_id="f1")
        with self.assertRaises(TransferConfigurationError) as ctx:
            validate_folder_hierarchy([f1], [])
        self.assertIn("self_parent", str(ctx.exception))

    def test_two_folder_cycle_raises_error(self) -> None:
        f1 = _make_folder_rec("f1", "F1", parent_id="f2")
        f2 = _make_folder_rec("f2", "F2", parent_id="f1")
        with self.assertRaises(TransferConfigurationError) as ctx:
            validate_folder_hierarchy([f1, f2], [])
        self.assertIn("cycle_detected", str(ctx.exception))

    def test_multi_folder_cycle_raises_error(self) -> None:
        f1 = _make_folder_rec("f1", "F1", parent_id="f3")
        f2 = _make_folder_rec("f2", "F2", parent_id="f1")
        f3 = _make_folder_rec("f3", "F3", parent_id="f2")
        with self.assertRaises(TransferConfigurationError) as ctx:
            validate_folder_hierarchy([f1, f2, f3], [])
        self.assertIn("cycle_detected", str(ctx.exception))

    def test_playlist_referencing_nonexistent_folder_raises_error(self) -> None:
        p = playlist("Playlist 1", [], identifier="p1", folder_id="ghost_folder")
        with self.assertRaises(TransferConfigurationError) as ctx:
            validate_folder_hierarchy([], [p])
        self.assertIn("playlist_missing_folder", str(ctx.exception))

    def test_valid_nested_folder_hierarchy_sorted_topologically(self) -> None:
        # Given child f2 before parent f1 in input list
        f2 = _make_folder_rec("f2", "Child", parent_id="f1")
        f1 = _make_folder_rec("f1", "Parent", parent_id=None)
        f3 = _make_folder_rec("f3", "Grandchild", parent_id="f2")
        sorted_folders = validate_folder_hierarchy([f2, f3, f1], [])
        ids = [f.source_id for f in sorted_folders]
        self.assertEqual(ids, ["f1", "f2", "f3"])

    def test_incomplete_folders_section_fails_planning(self) -> None:
        planner = TransferPlanner()
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.PLAYLISTS,))
        snap = snapshot(
            playlists=(),
            folders=(),
            incomplete_sections=["folders"],
        )
        dst = FakePlatformAdapter()
        with self.assertRaises(TransferConfigurationError) as ctx:
            planner.build(job, snap, ReadOnlyAdapter(dst))
        self.assertIn("source_section_incomplete:folders", str(ctx.exception))


class FolderPlanningTests(unittest.TestCase):
    """Section 42: Planning of folders and container binding."""

    def test_folder_item_planning_and_intent_binding(self) -> None:
        planner = TransferPlanner()
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.PLAYLISTS,))
        f_root = _make_folder_rec("f_root", "Root Folder", parent_id=None)
        f_child = _make_folder_rec("f_child", "Child Folder", parent_id="f_root")
        p_root = playlist(
            "Root Playlist",
            [track("T1", identifier="t1")],
            identifier="p_root",
            folder_id=None,
        )
        p_nested = playlist(
            "Nested Playlist",
            [track("T2", identifier="t2")],
            identifier="p_nested",
            folder_id="f_child",
        )
        snap = snapshot(
            playlists=(p_root, p_nested),
            folders=(f_root, f_child),
        )
        dst = FakePlatformAdapter()
        result = planner.build(job, snap, ReadOnlyAdapter(dst))

        by_source = {it.source_id: it for it in result.items}

        # Folder item uses CREATE_FOLDER
        item_f_root = by_source["f_root"]
        self.assertEqual(item_f_root.operation, TransferOperation.CREATE_FOLDER)
        self.assertIsNone(item_f_root.destination_id)
        self.assertIsNone(item_f_root.container_source_id)
        self.assertIsNone(item_f_root.container_destination_id)

        item_f_child = by_source["f_child"]
        self.assertEqual(item_f_child.operation, TransferOperation.CREATE_FOLDER)
        self.assertIsNone(item_f_child.destination_id)
        self.assertEqual(item_f_child.container_source_id, "f_root")
        self.assertIsNone(item_f_child.container_destination_id)

        # Playlist bindings
        item_p_root = by_source["p_root"]
        self.assertEqual(item_p_root.operation, TransferOperation.CREATE_PLAYLIST)
        self.assertIsNone(item_p_root.container_source_id)
        self.assertIsNone(item_p_root.container_destination_id)

        item_p_nested = by_source["p_nested"]
        self.assertEqual(item_p_nested.operation, TransferOperation.CREATE_PLAYLIST)
        self.assertEqual(item_p_nested.container_source_id, "f_child")
        self.assertIsNone(item_p_nested.container_destination_id)

    def test_changing_folder_or_playlist_parent_invalidates_plan_hash(self) -> None:
        plan_item1 = TransferPlanItem(
            entity_type=EntityType.FOLDER,
            source_id="f_child",
            destination_id=None,
            operation=TransferOperation.CREATE_FOLDER,
            planned_status=ItemStatus.PENDING,
            match_method=MatchMethod.NONE,
            match_score=0.0,
            original_position=0,
            write_position=None,
            container_source_id="f_root1",
            source_metadata={},
        )
        plan1 = TransferPlan(
            job_id="j1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            items=(plan_item1,),
        )

        plan_item2 = TransferPlanItem(
            entity_type=EntityType.FOLDER,
            source_id="f_child",
            destination_id=None,
            operation=TransferOperation.CREATE_FOLDER,
            planned_status=ItemStatus.PENDING,
            match_method=MatchMethod.NONE,
            match_score=0.0,
            original_position=0,
            write_position=None,
            container_source_id="f_root2",  # Changed parent!
            source_metadata={},
        )
        plan2 = TransferPlan(
            job_id="j1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            items=(plan_item2,),
        )

        self.assertNotEqual(plan1.compute_hash(), plan2.compute_hash())


class ExecutionAndContainerBindingTests(unittest.TestCase):
    """Section 44, 45, 46: Execution order, runtime binding, durable intent, and blocking."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.job_repo = JsonTransferJobRepository(Path(self.tmp_dir))
        self.item_repo = JsonTransferItemRepository(Path(self.tmp_dir))
        self.service = TransferService(self.job_repo, self.item_repo)

    def test_topological_write_order_and_runtime_destination_binding(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        f1 = _make_folder_rec("f1", "Root Folder", parent_id=None)
        f2 = _make_folder_rec("f2", "Sub Folder", parent_id="f1")
        p1 = playlist(
            "Foldered Playlist",
            [track("T1", identifier="t1")],
            identifier="p1",
            folder_id="f2",
        )
        source = FakePlatformAdapter(
            display_name="source",
            folders=[f2, f1],  # Given out of order
            playlists=[p1],
        )
        destination = FakePlatformAdapter(display_name="destination")

        self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=job.active_plan_id,
            revision=job.active_plan_revision,
            plan_hash=job.active_plan_hash,
        )
        result = self.service.execute(job, destination, confirmed=True)
        self.assertEqual(result["outcome"].succeeded, 4)  # f1, f2, p1, t1

        # Check call order
        calls = destination.write_calls
        f1_call = next(c for c in calls if c[0] == "create_folder" and c[1][0] == "Root Folder")
        f2_call = next(c for c in calls if c[0] == "create_folder" and c[1][0] == "Sub Folder")
        p1_call = next(c for c in calls if c[0] == "create_playlist")
        t1_call = next(c for c in calls if c[0] == "add_playlist_item")

        # Root folder created with None parent
        self.assertIsNone(f1_call[1][1])
        # Sub folder created with f1 destination ID as parent
        f1_dst_id = next(it.destination_id for it in self.service.items.list_for_job(job.id) if it.source_id == "f1")
        f2_dst_id = next(it.destination_id for it in self.service.items.list_for_job(job.id) if it.source_id == "f2")
        self.assertEqual(f2_call[1][1], f1_dst_id)

        # Playlist created in f2
        created_p = destination.playlists[0]
        self.assertEqual(created_p.folder_id, f2_dst_id)

        # Order: f1 before f2, f2 before p1, p1 before t1
        self.assertLess(calls.index(f1_call), calls.index(f2_call))
        self.assertLess(calls.index(f2_call), calls.index(p1_call))
        self.assertLess(calls.index(p1_call), calls.index(t1_call))

    def test_folder_failure_blocks_subtree_and_prevents_root_fallback(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        f_fail = _make_folder_rec("f_fail", "Failed Folder", parent_id=None)
        f_sub = _make_folder_rec("f_sub", "Sub Folder", parent_id="f_fail")
        p_sub = playlist(
            "Sub Playlist",
            [track("T1", identifier="t1")],
            identifier="p_sub",
            folder_id="f_sub",
        )
        f_ok = _make_folder_rec("f_ok", "OK Sibling", parent_id=None)
        p_ok = playlist(
            "OK Playlist",
            [track("T2", identifier="t2")],
            identifier="p_ok",
            folder_id="f_ok",
        )

        source = FakePlatformAdapter(
            display_name="source",
            folders=[f_fail, f_sub, f_ok],
            playlists=[p_sub, p_ok],
        )
        destination = FakePlatformAdapter(
            display_name="destination",
        )
        orig_create_folder = destination.create_folder

        def conditional_create_folder(name: str, parent_id: str | None = None) -> str:
            if name == "Failed Folder":
                destination._record("create_folder", name, parent_id)
                raise TemporaryPlatformError("simulated_folder_failure")
            return orig_create_folder(name, parent_id)

        destination.create_folder = conditional_create_folder  # type: ignore[method-assign]

        self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=job.active_plan_id,
            revision=job.active_plan_revision,
            plan_hash=job.active_plan_hash,
        )
        self.service.execute(job, destination, confirmed=True)

        items_by_src = {it.source_id: it for it in self.service.items.list_for_job(job.id)}

        # f_fail failed or ambiguous
        self.assertIn(items_by_src["f_fail"].status, (ItemStatus.FAILED, ItemStatus.AMBIGUOUS))

        # f_sub blocked
        self.assertEqual(items_by_src["f_sub"].status, ItemStatus.AMBIGUOUS)
        self.assertEqual(items_by_src["f_sub"].last_error, "container_blocked")

        # p_sub blocked - NEVER created in root!
        self.assertEqual(items_by_src["p_sub"].status, ItemStatus.AMBIGUOUS)
        self.assertEqual(items_by_src["p_sub"].last_error, "container_blocked")

        # t1 blocked
        self.assertEqual(items_by_src["t1"].status, ItemStatus.AMBIGUOUS)
        self.assertIn(items_by_src["t1"].last_error, ("container_blocked", "playlist_sequence_blocked"))

        # Sibling f_ok and p_ok succeeded!
        self.assertEqual(items_by_src["f_ok"].status, ItemStatus.TRANSFERRED)
        self.assertEqual(items_by_src["p_ok"].status, ItemStatus.TRANSFERRED)
        self.assertEqual(items_by_src["t2"].status, ItemStatus.TRANSFERRED)

        # Ensure p_sub was never created on destination
        self.assertEqual(len(destination.playlists), 1)
        self.assertEqual(destination.playlists[0].name, "OK Playlist")

    def test_folder_reconciliation_recovers_transferred_when_single_match_exists(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        f1 = _make_folder_rec("f1", "Reconciled Folder", parent_id=None)
        source = FakePlatformAdapter(display_name="source", folders=[f1], playlists=[])
        destination = FakePlatformAdapter(display_name="destination")

        def timeout_after_folder_create(name: str, parent_id: str | None = None) -> str:
            fid = "dst-existing-fld-1"
            destination.folders.append(_make_folder_rec(fid, name, parent_id=parent_id))
            raise TemporaryPlatformError("network_timeout_after_folder_commit")

        destination.create_folder = timeout_after_folder_create  # type: ignore[method-assign]

        self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=job.active_plan_id,
            revision=job.active_plan_revision,
            plan_hash=job.active_plan_hash,
        )
        result = self.service.execute(job, destination, confirmed=True)
        self.assertEqual(result["outcome"].succeeded, 1)

        f_item = next(it for it in self.service.items.list_for_job(job.id) if it.source_id == "f1")
        self.assertEqual(f_item.status, ItemStatus.TRANSFERRED)
        self.assertEqual(f_item.destination_id, "dst-existing-fld-1")
        self.assertEqual(f_item.mutation_state, MutationState.NONE)

    def test_folder_reconciliation_fails_ambiguous_when_destination_has_zero_or_multiple_matches(self) -> None:
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.PLAYLISTS,))
        f1 = _make_folder_rec("f1", "Ambiguous Folder", parent_id=None)
        source = FakePlatformAdapter(display_name="source", folders=[f1], playlists=[])
        destination = FakePlatformAdapter(display_name="destination")

        def timeout_without_creating(name: str, parent_id: str | None = None) -> str:
            raise TemporaryPlatformError("network_timeout_no_commit")

        destination.create_folder = timeout_without_creating  # type: ignore[method-assign]

        self.service.analyze(job, source, destination)
        self.service.confirm_plan(
            job,
            plan_id=job.active_plan_id,
            revision=job.active_plan_revision,
            plan_hash=job.active_plan_hash,
        )
        result = self.service.execute(job, destination, confirmed=True)
        self.assertEqual(result["outcome"].failed, 1)

        f_item = next(it for it in self.service.items.list_for_job(job.id) if it.source_id == "f1")
        self.assertEqual(f_item.status, ItemStatus.AMBIGUOUS)


class VerificationTests(unittest.TestCase):
    """Section 47, 48: Verification of folder hierarchy and playlist placement."""

    def test_successful_folder_and_playlist_placement_verification(self) -> None:
        dest = FakePlatformAdapter()
        dest.folders = [
            _make_folder_rec("dst-f1", "Folder 1", parent_id=None),
            _make_folder_rec("dst-f2", "Subfolder 2", parent_id="dst-f1"),
        ]
        dest.playlists = [
            playlist(
                "Pl 1",
                [track("T1", identifier="dst-t1")],
                identifier="dst-p1",
                folder_id="dst-f2",
            )
        ]

        f1_item = TransferItem.create(
            "j1", EntityType.FOLDER, Platform.TIDAL, "f1", Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder 1"},
        )
        f1_item.destination_id = "dst-f1"
        f1_item.status = ItemStatus.TRANSFERRED

        f2_item = TransferItem.create(
            "j1", EntityType.FOLDER, Platform.TIDAL, "f2", Platform.TIDAL,
            container_source_id="f1",
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Subfolder 2"},
        )
        f2_item.destination_id = "dst-f2"
        f2_item.container_destination_id = "dst-f1"
        f2_item.status = ItemStatus.TRANSFERRED

        p1_item = TransferItem.create(
            "j1", EntityType.PLAYLIST, Platform.TIDAL, "p1", Platform.TIDAL,
            container_source_id="f2",
            operation=TransferOperation.CREATE_PLAYLIST,
            source_metadata={"name": "Pl 1"},
        )
        p1_item.destination_id = "dst-p1"
        p1_item.container_destination_id = "dst-f2"
        p1_item.status = ItemStatus.TRANSFERRED

        t1_item = TransferItem.create(
            "j1", EntityType.PLAYLIST_ITEM, Platform.TIDAL, "t1", Platform.TIDAL,
            container_source_id="p1",
            original_position=0,
            write_position=0,
            operation=TransferOperation.ADD_PLAYLIST_ITEM,
        )
        t1_item.destination_id = "dst-t1"
        t1_item.container_destination_id = "dst-p1"
        t1_item.status = ItemStatus.TRANSFERRED

        verifier = TransferVerifier(dest)
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.PLAYLISTS,))
        results = verifier.verify_job(job, [f1_item, f2_item, p1_item, t1_item])

        self.assertTrue(results["folder:dst-f1"].success)
        self.assertTrue(results["folder:dst-f2"].success)
        self.assertTrue(results["playlist_placement:dst-p1"].success)
        self.assertEqual(aggregate_verification_status(results), VerificationStatus.PASSED)

    def test_playlist_folder_mismatch_fails_verification(self) -> None:
        dest = FakePlatformAdapter()
        dest.folders = [_make_folder_rec("dst-f1", "Folder 1", parent_id=None)]
        dest.playlists = [
            playlist(
                "Pl 1",
                [],
                identifier="dst-p1",
                folder_id=None,  # Landed in root instead of dst-f1!
            )
        ]

        f1_item = TransferItem.create(
            "j1", EntityType.FOLDER, Platform.TIDAL, "f1", Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder 1"},
        )
        f1_item.destination_id = "dst-f1"
        f1_item.status = ItemStatus.TRANSFERRED

        p1_item = TransferItem.create(
            "j1", EntityType.PLAYLIST, Platform.TIDAL, "p1", Platform.TIDAL,
            container_source_id="f1",
            operation=TransferOperation.CREATE_PLAYLIST,
            source_metadata={"name": "Pl 1"},
        )
        p1_item.destination_id = "dst-p1"
        p1_item.container_destination_id = "dst-f1"
        p1_item.status = ItemStatus.TRANSFERRED

        verifier = TransferVerifier(dest)
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.PLAYLISTS,))
        results = verifier.verify_job(job, [f1_item, p1_item])

        placement_result = results["playlist_placement:dst-p1"]
        self.assertFalse(placement_result.success)
        self.assertIn("playlist_folder_mismatch", placement_result.missing)
        self.assertEqual(aggregate_verification_status(results), VerificationStatus.FAILED)

    def test_incomplete_folder_verification_results_in_partial_status(self) -> None:
        dest = FakePlatformAdapter()
        dest.folders = [_make_folder_rec("dst-f1", "Folder 1", parent_id=None)]

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

        f1_item = TransferItem.create(
            "j1", EntityType.FOLDER, Platform.TIDAL, "f1", Platform.TIDAL,
            operation=TransferOperation.CREATE_FOLDER,
            source_metadata={"name": "Folder 1"},
        )
        f1_item.destination_id = "dst-f1"
        f1_item.status = ItemStatus.TRANSFERRED

        verifier = TransferVerifier(dest)
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.PLAYLISTS,))
        results = verifier.verify_job(job, [f1_item])

        self.assertEqual(aggregate_verification_status(results), VerificationStatus.PARTIAL)


class TidalAdapterFolderTests(unittest.TestCase):
    """Section 49: TIDAL adapter allowlist and folder root mapping."""

    def test_can_reuse_identifier_allowlist_for_every_entity_type(self) -> None:
        client = MagicMock()
        adapter = TidalAdapter(client)

        reusable_expected = {
            EntityType.TRACK: True,
            EntityType.ALBUM: True,
            EntityType.ARTIST: True,
            EntityType.VIDEO: True,
            EntityType.MIX: True,
            EntityType.PLAYLIST: False,
            EntityType.PLAYLIST_ITEM: False,
            EntityType.FOLDER: False,
        }

        for entity_type, expected in reusable_expected.items():
            with self.subTest(entity_type=entity_type):
                actual = adapter.can_reuse_identifier(entity_type, Platform.TIDAL)
                self.assertEqual(actual, expected)
                # Non-TIDAL platform is always False
                self.assertFalse(adapter.can_reuse_identifier(entity_type, Platform.SPOTIFY))

    def test_create_folder_translates_none_to_root(self) -> None:
        client = MagicMock()
        client.create_folder.return_value = "fld-123"
        adapter = TidalAdapter(client)

        result = adapter.create_folder("My Folder", None)
        self.assertEqual(result, "fld-123")
        client.create_folder.assert_called_once_with("My Folder", "root")

    def test_create_folder_error_translation_is_hardened(self) -> None:
        client = MagicMock()
        client.create_folder.side_effect = TidalClientError(reason="api_timeout")
        adapter = TidalAdapter(client)

        with self.assertRaises(AmbiguousOperationError):
            adapter.create_folder("My Folder", None)

        client.create_folder.side_effect = TidalClientError(reason="provider_error")
        with self.assertRaises(PermanentPlatformError):
            adapter.create_folder("My Folder", None)

    def test_mapper_normalizes_root_to_none(self) -> None:
        raw_fld = MagicMock()
        raw_fld.id = "f1"
        raw_fld.name = "Root Folder"
        raw_fld.created = None

        rec_root = folder_record_from_tidal(raw_fld, "root")
        self.assertIsNone(folder_parent_source_id(rec_root))

        rec_none = folder_record_from_tidal(raw_fld, None)
        self.assertIsNone(folder_parent_source_id(rec_none))

        rec_child = folder_record_from_tidal(raw_fld, "f_parent")
        self.assertEqual(folder_parent_source_id(rec_child), "f_parent")

        raw_pl = MagicMock()
        raw_pl.id = "p1"
        raw_pl.name = "Pl 1"
        raw_pl.description = None
        raw_pl.public = True
        raw_pl.created = None
        raw_pl.duration = 100
        raw_pl.num_tracks = 0

        pl_root = playlist_from_tidal(raw_pl, folder_id="root")
        self.assertIsNone(pl_root.folder_id)

        pl_child = playlist_from_tidal(raw_pl, folder_id="f_parent")
        self.assertEqual(pl_child.folder_id, "f_parent")

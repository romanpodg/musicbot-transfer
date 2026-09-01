"""Comprehensive unit and regression tests for Phase 1.5B.1: Set-Like Verification Subset Semantics.

Validates:
1. compare_expected_membership subset semantics (expected ⊆ actual).
2. Deduplication of expected IDs for set semantics.
3. Actual count defined as observed expected IDs, not total destination collection size.
4. Unrelated destination content never populates unexpected or causes verification failure for set sections.
5. Missing expected IDs cause verification failure across all 5 set-like types (tracks, albums, artists, videos, mixes).
6. Both TRANSFERRED and ALREADY_EXISTS items are verified.
7. Incomplete destination state and unsupported capabilities produce failure/warning (aggregate PARTIAL).
8. Playlist verification remains exact for membership, duplicate counts, and ordering.
9. Cross-section isolation between set-like sections.
10. End-to-end service transfer passes when destination holds unrelated pre-existing content.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from music_transfer.app.services import TransferService
from music_transfer.core.domain import (
    Account,
    TransferItem,
    TransferJob,
)
from music_transfer.core.enums import (
    ContentType,
    EntityType,
    ItemStatus,
    Platform,
    TransferOperation,
    VerificationStatus,
)
from music_transfer.core.errors import UnsupportedCapabilityError
from music_transfer.core.ports import DestinationState
from music_transfer.core.transfer import (
    TransferVerifier,
    aggregate_verification_status,
    compare_expected_membership,
)
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
    JsonTransferPlanRepository,
)

from tests.support import FakePlatformAdapter, record


def _make_account(account_id: str, platform: Platform = Platform.TIDAL) -> Account:
    return Account(
        id=account_id,
        platform=platform,
        platform_account_id=f"{platform.value}-{account_id}",
        display_name=f"Account {account_id}",
    )


def _make_item(
    item_id: str,
    job_id: str,
    entity_type: EntityType,
    source_id: str,
    destination_id: str | None = None,
    status: ItemStatus = ItemStatus.PENDING,
    operation: TransferOperation = TransferOperation.NONE,
    source_platform: Platform = Platform.TIDAL,
    destination_platform: Platform = Platform.TIDAL,
) -> TransferItem:
    return TransferItem(
        id=item_id,
        job_id=job_id,
        entity_type=entity_type,
        source_platform=source_platform,
        source_id=source_id,
        destination_platform=destination_platform,
        destination_id=destination_id,
        status=status,
        operation=operation,
    )


class SetVerificationMembershipHelperTests(unittest.TestCase):
    """Unit tests for compare_expected_membership helper."""

    def test_set_verification_expected_subset_passes(self) -> None:
        """When expected IDs are a subset of actual IDs (expected ⊆ actual), verification passes."""
        result = compare_expected_membership(
            expected=["id-1", "id-2"],
            actual=["id-1", "id-2", "unrelated-3", "unrelated-4"],
        )
        self.assertTrue(result.success)
        self.assertEqual(result.expected_count, 2)
        self.assertEqual(result.actual_count, 2)
        self.assertEqual(result.missing, [])
        self.assertEqual(result.unexpected, [])
        self.assertEqual(result.order_mismatches, [])

    def test_set_verification_missing_expected_id_fails(self) -> None:
        """When any expected ID is absent from destination, verification fails with missing populated."""
        result = compare_expected_membership(
            expected=["id-1", "id-2"],
            actual=["id-1", "unrelated-3"],
        )
        self.assertFalse(result.success)
        self.assertEqual(result.expected_count, 2)
        self.assertEqual(result.actual_count, 1)
        self.assertEqual(result.missing, ["id-2"])
        self.assertEqual(result.unexpected, [])

    def test_set_verification_deduplicates_expected_ids(self) -> None:
        """Duplicate IDs in expected input are deduplicated for set semantics."""
        result = compare_expected_membership(
            expected=["id-1", "id-1", "id-2"],
            actual=["id-1", "id-2", "unrelated-3"],
        )
        self.assertTrue(result.success)
        self.assertEqual(result.expected_count, 2)
        self.assertEqual(result.actual_count, 2)
        self.assertEqual(result.missing, [])
        self.assertEqual(result.unexpected, [])

    def test_set_verification_unrelated_items_never_populate_unexpected(self) -> None:
        """Unrelated items in actual collection never appear in the unexpected list."""
        result = compare_expected_membership(
            expected=["id-1"],
            actual=["id-1", "unrelated-a", "unrelated-b", "unrelated-c"],
        )
        self.assertTrue(result.success)
        self.assertEqual(result.unexpected, [])
        self.assertEqual(result.actual_count, 1)


class SetLikeSectionVerificationTests(unittest.TestCase):
    """Tests for TransferVerifier across all set-like sections with unrelated destination content."""

    def test_track_verification_ignores_unrelated_destination_items(self) -> None:
        """Liked tracks verification passes when destination has unrelated existing tracks."""
        dest = FakePlatformAdapter(
            tracks=[record("t-expected", "Expected Track"), record("t-unrelated", "Unrelated Track")]
        )
        verifier = TransferVerifier(dest)
        result = verifier.verify_liked_tracks(["t-expected"])
        self.assertTrue(result.success)
        self.assertEqual(result.expected_count, 1)
        self.assertEqual(result.actual_count, 1)
        self.assertEqual(result.missing, [])
        self.assertEqual(result.unexpected, [])

    def test_album_verification_ignores_unrelated_destination_items(self) -> None:
        """Saved albums verification passes when destination has unrelated existing albums."""
        dest = FakePlatformAdapter(
            albums=[record("al-expected", "Expected Album"), record("al-unrelated", "Unrelated Album")]
        )
        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.SAVED_ALBUMS,),
        )
        items = [
            _make_item(
                "it-1",
                "job-1",
                EntityType.ALBUM,
                "al-expected",
                destination_id="al-expected",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_ALBUM,
            )
        ]
        results = verifier.verify_job(job, items)
        self.assertIn("albums", results)
        al_res = results["albums"]
        self.assertTrue(al_res.success)
        self.assertEqual(al_res.expected_count, 1)
        self.assertEqual(al_res.actual_count, 1)
        self.assertEqual(al_res.missing, [])
        self.assertEqual(al_res.unexpected, [])

    def test_artist_verification_ignores_unrelated_destination_items(self) -> None:
        """Followed artists verification passes when destination has unrelated existing artists."""
        dest = FakePlatformAdapter(
            artists=[record("ar-expected", "Expected Artist"), record("ar-unrelated", "Unrelated Artist")]
        )
        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.FOLLOWED_ARTISTS,),
        )
        items = [
            _make_item(
                "it-1",
                "job-1",
                EntityType.ARTIST,
                "ar-expected",
                destination_id="ar-expected",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.FOLLOW_ARTIST,
            )
        ]
        results = verifier.verify_job(job, items)
        self.assertIn("artists", results)
        ar_res = results["artists"]
        self.assertTrue(ar_res.success)
        self.assertEqual(ar_res.expected_count, 1)
        self.assertEqual(ar_res.actual_count, 1)
        self.assertEqual(ar_res.missing, [])
        self.assertEqual(ar_res.unexpected, [])

    def test_video_verification_ignores_unrelated_destination_items(self) -> None:
        """Videos verification passes when destination has unrelated existing videos."""
        dest = FakePlatformAdapter(
            videos=[record("v-expected", "Expected Video"), record("v-unrelated", "Unrelated Video")]
        )
        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.VIDEOS,),
        )
        items = [
            _make_item(
                "it-1",
                "job-1",
                EntityType.VIDEO,
                "v-expected",
                destination_id="v-expected",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_VIDEO,
            )
        ]
        results = verifier.verify_job(job, items)
        self.assertIn("videos", results)
        v_res = results["videos"]
        self.assertTrue(v_res.success)
        self.assertEqual(v_res.expected_count, 1)
        self.assertEqual(v_res.actual_count, 1)
        self.assertEqual(v_res.missing, [])
        self.assertEqual(v_res.unexpected, [])

    def test_mix_verification_ignores_unrelated_destination_items(self) -> None:
        """Mixes verification passes when destination has unrelated existing mixes."""
        dest = FakePlatformAdapter(
            mixes=[record("m-expected", "Expected Mix"), record("m-unrelated", "Unrelated Mix")]
        )
        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.MIXES,),
        )
        items = [
            _make_item(
                "it-1",
                "job-1",
                EntityType.MIX,
                "m-expected",
                destination_id="m-expected",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_MIX,
            )
        ]
        results = verifier.verify_job(job, items)
        self.assertIn("mixes", results)
        m_res = results["mixes"]
        self.assertTrue(m_res.success)
        self.assertEqual(m_res.expected_count, 1)
        self.assertEqual(m_res.actual_count, 1)
        self.assertEqual(m_res.missing, [])
        self.assertEqual(m_res.unexpected, [])

    def test_video_verification_missing_expected_id_fails(self) -> None:
        """Video verification fails when expected video is not in destination."""
        dest = FakePlatformAdapter(videos=[record("v-other", "Other Video")])
        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.VIDEOS,),
        )
        items = [
            _make_item(
                "it-1",
                "job-1",
                EntityType.VIDEO,
                "v-expected",
                destination_id="v-expected",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_VIDEO,
            )
        ]
        results = verifier.verify_job(job, items)
        v_res = results["videos"]
        self.assertFalse(v_res.success)
        self.assertEqual(v_res.missing, ["v-expected"])
        self.assertEqual(v_res.unexpected, [])

    def test_mix_verification_missing_expected_id_fails(self) -> None:
        """Mix verification fails when expected mix is not in destination."""
        dest = FakePlatformAdapter(mixes=[record("m-other", "Other Mix")])
        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.MIXES,),
        )
        items = [
            _make_item(
                "it-1",
                "job-1",
                EntityType.MIX,
                "m-expected",
                destination_id="m-expected",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_MIX,
            )
        ]
        results = verifier.verify_job(job, items)
        m_res = results["mixes"]
        self.assertFalse(m_res.success)
        self.assertEqual(m_res.missing, ["m-expected"])
        self.assertEqual(m_res.unexpected, [])

    def test_transferred_and_already_existing_ids_are_both_verified(self) -> None:
        """Both TRANSFERRED and ALREADY_EXISTS items must exist at the destination."""
        dest = FakePlatformAdapter(
            tracks=[
                record("t1", "Track 1"),
                record("t2", "Track 2"),
                record("t-unrelated", "Unrelated"),
            ]
        )
        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.LIKED_TRACKS,),
        )
        items = [
            _make_item(
                "it-1",
                "job-1",
                EntityType.TRACK,
                "t1",
                destination_id="t1",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_TRACK,
            ),
            _make_item(
                "it-2",
                "job-1",
                EntityType.TRACK,
                "t2",
                destination_id="t2",
                status=ItemStatus.ALREADY_EXISTS,
                operation=TransferOperation.NONE,
            ),
        ]
        results = verifier.verify_job(job, items)
        self.assertTrue(results["tracks"].success)
        self.assertEqual(results["tracks"].expected_count, 2)
        self.assertEqual(results["tracks"].actual_count, 2)

        # When one of them (e.g. t2) is missing from destination, verification fails
        dest_missing = FakePlatformAdapter(
            tracks=[record("t1", "Track 1"), record("t-unrelated", "Unrelated")]
        )
        verifier_missing = TransferVerifier(dest_missing)
        results_missing = verifier_missing.verify_job(job, items)
        self.assertFalse(results_missing["tracks"].success)
        self.assertEqual(results_missing["tracks"].missing, ["t2"])
        self.assertEqual(results_missing["tracks"].actual_count, 1)

    def test_cross_section_isolation_with_unrelated_items(self) -> None:
        """Unrelated items in multiple sections do not interfere across sections."""
        dest = FakePlatformAdapter(
            tracks=[record("t1", "Track 1"), record("t-extra", "Extra Track")],
            videos=[record("v1", "Video 1"), record("v-extra", "Extra Video")],
        )
        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.LIKED_TRACKS, ContentType.VIDEOS),
        )
        items = [
            _make_item(
                "it-1",
                "job-1",
                EntityType.TRACK,
                "t1",
                destination_id="t1",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_TRACK,
            ),
            _make_item(
                "it-2",
                "job-1",
                EntityType.VIDEO,
                "v1",
                destination_id="v1",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_VIDEO,
            ),
        ]
        results = verifier.verify_job(job, items)
        self.assertTrue(results["tracks"].success)
        self.assertTrue(results["videos"].success)
        self.assertEqual(aggregate_verification_status(results), VerificationStatus.PASSED)


class IncompleteAndUnsupportedVerificationTests(unittest.TestCase):
    """Tests for incomplete destination state and unsupported capability handling."""

    def test_incomplete_destination_state_is_not_passed(self) -> None:
        """Incomplete destination state marks section as failed and produces destination_state_incomplete warning."""
        class IncompleteDestination(FakePlatformAdapter):
            def get_destination_state(self, sections: tuple[str, ...] | None = None) -> DestinationState:
                return DestinationState(
                    platform=Platform.TIDAL,
                    track_ids=frozenset({"t1"}),
                    incomplete_sections=("tracks",),
                )

        dest = IncompleteDestination()
        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.LIKED_TRACKS,),
        )
        items = [
            _make_item(
                "it-1",
                "job-1",
                EntityType.TRACK,
                "t1",
                destination_id="t1",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_TRACK,
            )
        ]
        results = verifier.verify_job(job, items)
        self.assertFalse(results["tracks"].success)
        self.assertEqual(results["tracks"].warnings, ["destination_state_incomplete"])
        self.assertEqual(results["tracks"].actual_count, 1)
        self.assertEqual(aggregate_verification_status(results), VerificationStatus.PARTIAL)

    def test_unsupported_read_capability_is_not_passed(self) -> None:
        """Unsupported read capability produces verification_unsupported warning and PARTIAL aggregate status."""
        class UnsupportedReadDestination(FakePlatformAdapter):
            def get_destination_state(self, sections: tuple[str, ...] | None = None) -> DestinationState:
                raise UnsupportedCapabilityError("capability_unsupported", capability="read_videos")

        dest = UnsupportedReadDestination()
        verifier = TransferVerifier(dest)
        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.VIDEOS,),
        )
        items = [
            _make_item(
                "it-1",
                "job-1",
                EntityType.VIDEO,
                "v1",
                destination_id="v1",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_VIDEO,
            )
        ]
        results = verifier.verify_job(job, items)
        self.assertFalse(results["videos"].success)
        self.assertEqual(results["videos"].warnings, ["verification_unsupported:read_videos"])
        self.assertEqual(aggregate_verification_status(results), VerificationStatus.PARTIAL)


class PlaylistVerificationExactContractTests(unittest.TestCase):
    """Tests confirming playlist sequence verification remains exact in membership, counts, and ordering."""

    def test_playlist_verification_still_rejects_unexpected_item(self) -> None:
        """Playlist verification fails when the destination container contains unexpected items."""
        dest = FakePlatformAdapter()
        dest.playlist_item_ids = lambda pl_id: ["t1", "t2", "t-extra"]  # type: ignore[method-assign]
        verifier = TransferVerifier(dest)
        result = verifier.verify_playlist("pl-dst", ["t1", "t2"])
        self.assertFalse(result.success)
        self.assertEqual(result.expected_count, 2)
        self.assertEqual(result.actual_count, 3)
        self.assertEqual(result.unexpected, ["t-extra"])
        self.assertEqual(result.missing, [])

    def test_playlist_duplicate_verification_unchanged(self) -> None:
        """Playlist multiset comparison requires exact duplicate occurrences."""
        dest = FakePlatformAdapter()
        dest.playlist_item_ids = lambda pl_id: ["t1", "t2"]  # type: ignore[method-assign]
        verifier = TransferVerifier(dest)
        # Expected contains duplicate t1, actual has only one t1
        result = verifier.verify_playlist("pl-dst", ["t1", "t1", "t2"])
        self.assertFalse(result.success)
        self.assertEqual(result.missing, ["t1"])

    def test_playlist_order_verification_unchanged(self) -> None:
        """Playlist sequence comparison rejects reversed/mismatched item ordering."""
        dest = FakePlatformAdapter()
        dest.playlist_item_ids = lambda pl_id: ["t2", "t1"]  # type: ignore[method-assign]
        verifier = TransferVerifier(dest)
        result = verifier.verify_playlist("pl-dst", ["t1", "t2"])
        self.assertFalse(result.success)
        self.assertTrue(len(result.order_mismatches) > 0)


class EndToEndSetVerificationTests(unittest.TestCase):
    """End-to-end service transfer verification with pre-existing unrelated destination items."""

    def _setup_service(self) -> tuple[TransferService, Path]:
        root = Path(tempfile.mkdtemp())
        jobs_repo = JsonTransferJobRepository(root)
        items_repo = JsonTransferItemRepository(root)
        plans_repo = JsonTransferPlanRepository(root)
        service = TransferService(
            jobs_repo,
            items_repo,
            plans_repository=plans_repo,
        )
        return service, root

    def test_end_to_end_video_transfer_passes_with_unrelated_existing_video(self) -> None:
        """Service transfer of new video succeeds and passes verification when destination has unrelated existing video."""
        service, _ = self._setup_service()
        src_account = _make_account("src-1")
        dst_account = _make_account("dst-1")

        source = FakePlatformAdapter(videos=[record("v-new", "New Video")])
        destination = FakePlatformAdapter(videos=[record("v-unrelated", "Existing Video")])

        job = service.create_job(src_account, dst_account, content=(ContentType.VIDEOS,))
        plan = service.analyze(job, source, destination)
        service.confirm_plan(job, plan_id=plan.plan_id, revision=plan.revision, plan_hash=plan.plan_hash)

        result = service.execute(job, destination, confirmed=True)

        self.assertIn("v-new", destination.saved_videos)
        self.assertIn("v-unrelated", [v.source_id for v in destination.videos])
        report = result["report"]
        self.assertEqual(report.transferred, 1)

        verification = result["verification"]
        self.assertIn("videos", verification)
        self.assertTrue(verification["videos"]["success"])
        self.assertEqual(verification["videos"]["expected_count"], 1)
        self.assertEqual(verification["videos"]["actual_count"], 1)
        self.assertEqual(verification["videos"]["missing"], [])
        self.assertEqual(verification["videos"]["unexpected"], [])
        self.assertEqual(result["verification_status"], VerificationStatus.PASSED)
        self.assertEqual(job.verification_status, VerificationStatus.PASSED)

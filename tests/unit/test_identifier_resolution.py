"""Regression tests for Phase 1.5A declarative identifier resolution and cross-platform set-like matching.

Guardrails verified:
1. TransferContentSpec declares explicit operations, resolution policies, and search capabilities.
2. Reusable IDs (TIDAL -> TIDAL) do not trigger catalog searches.
3. Cross-platform transfers without ID portability use conservative catalog resolution.
4. Missing search capabilities or unmatched candidates result in explicit non-executable item statuses.
5. Ambiguous matches are flagged AMBIGUOUS with destination_id=None and are non-executable.
6. Destination presence queries and preconditions always use the resolved destination_id.
7. Central plan validation guarantees no executable set-like item has a missing destination identifier.
8. Confirmed plans do not rematch during execution or resume.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from music_transfer.app.services import TransferService
from music_transfer.core.domain import (
    TransferItem,
    TransferJob,
    TransferPlan,
    TransferPlanItem,
    TransferSettings,
)
from music_transfer.core.enums import (
    ContentType,
    EntityType,
    IdentifierResolutionPolicy,
    ItemStatus,
    JobStatus,
    MatchMethod,
    MatchOutcome,
    Platform,
    PreconditionExpectation,
    TransferOperation,
)
from music_transfer.core.errors import TransferConfigurationError
from music_transfer.core.matching import AlbumMatcher, ArtistMatcher, MatchingPolicy
from music_transfer.core.ports import PlatformCapabilities, ReadOnlyAdapter
from music_transfer.core.transfer import (
    TransferPlanner,
    require_transfer_content_spec,
    validate_plan_set_like_items,
)
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
    JsonTransferPlanRepository,
)

from tests.support import (
    FakePlatformAdapter,
    album,
    artist,
    snapshot,
)


def build_service(root: Path) -> TransferService:
    return TransferService(
        JsonTransferJobRepository(root),
        JsonTransferItemRepository(root),
        plans_repository=JsonTransferPlanRepository(root),
    )


class DeclarativeIdentifierResolutionTests(unittest.TestCase):
    """Test suite for Phase 1.5A declarative identifier resolution."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = build_service(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _confirm_items(self, job: TransferJob, items: list[TransferItem]) -> None:
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
                source_metadata=dict(it.source_metadata),
            )
            for it in items
        )
        plan = TransferPlan.create(job.id, revision=1, items=plan_items)
        assert self.service.plans is not None
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

    def test_set_like_spec_declares_transfer_operation(self) -> None:
        """TransferContentSpec declares operation, resolution_policy, and search_capability."""
        liked_tracks = require_transfer_content_spec(ContentType.LIKED_TRACKS)
        self.assertEqual(liked_tracks.operation, TransferOperation.SAVE_TRACK)
        self.assertEqual(
            liked_tracks.resolution_policy, IdentifierResolutionPolicy.REUSE_OR_SEARCH
        )
        self.assertEqual(liked_tracks.search_capability, "search_tracks")

        saved_albums = require_transfer_content_spec(ContentType.SAVED_ALBUMS)
        self.assertEqual(saved_albums.operation, TransferOperation.SAVE_ALBUM)
        self.assertEqual(
            saved_albums.resolution_policy, IdentifierResolutionPolicy.REUSE_OR_SEARCH
        )
        self.assertEqual(saved_albums.search_capability, "search_albums")

        followed_artists = require_transfer_content_spec(ContentType.FOLLOWED_ARTISTS)
        self.assertEqual(followed_artists.operation, TransferOperation.FOLLOW_ARTIST)
        self.assertEqual(
            followed_artists.resolution_policy, IdentifierResolutionPolicy.REUSE_OR_SEARCH
        )
        self.assertEqual(followed_artists.search_capability, "search_artists")

        playlists = require_transfer_content_spec(ContentType.PLAYLISTS)
        self.assertEqual(playlists.operation, TransferOperation.CREATE_PLAYLIST)
        self.assertEqual(
            playlists.resolution_policy, IdentifierResolutionPolicy.CONTAINER_CREATE
        )
        self.assertIsNone(playlists.search_capability)

    def test_tidal_album_direct_id_reuse_does_not_search(self) -> None:
        """TIDAL -> TIDAL album transfers reuse source IDs directly without catalog searches."""
        src_album = album("OK Computer", identifier="alb-1", artists=["Radiohead"])
        snap = snapshot(albums=(src_album,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.TIDAL,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.SAVED_ALBUMS,),
        )

        dst = FakePlatformAdapter(platform=Platform.TIDAL)
        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertEqual(item.entity_type, EntityType.ALBUM)
        self.assertEqual(item.operation, TransferOperation.SAVE_ALBUM)
        self.assertEqual(item.source_id, "alb-1")
        self.assertEqual(item.destination_id, "alb-1")
        self.assertEqual(item.match_method, MatchMethod.DIRECT_ID)
        self.assertEqual(item.match_score, 1.0)
        self.assertEqual(item.status, ItemStatus.MATCHED)

    def test_tidal_artist_direct_id_reuse_does_not_search(self) -> None:
        """TIDAL -> TIDAL artist transfers reuse source IDs directly without catalog searches."""
        src_artist = artist("Radiohead", identifier="art-1")
        snap = snapshot(artists=(src_artist,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.TIDAL,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.FOLLOWED_ARTISTS,),
        )

        dst = FakePlatformAdapter(platform=Platform.TIDAL)
        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertEqual(item.entity_type, EntityType.ARTIST)
        self.assertEqual(item.operation, TransferOperation.FOLLOW_ARTIST)
        self.assertEqual(item.source_id, "art-1")
        self.assertEqual(item.destination_id, "art-1")
        self.assertEqual(item.match_method, MatchMethod.DIRECT_ID)
        self.assertEqual(item.match_score, 1.0)
        self.assertEqual(item.status, ItemStatus.MATCHED)

    def test_cross_platform_album_uses_search_when_id_not_reusable(self) -> None:
        """Cross-platform album transfer without ID reuse searches destination and assigns resolved ID."""
        src_album = album("OK Computer", identifier="src-alb-1", artists=["Radiohead"])
        dst_album = album(
            "OK Computer",
            identifier="dst-alb-99",
            platform=Platform.SPOTIFY,
            artists=["Radiohead"],
        )

        snap = snapshot(albums=(src_album,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.SAVED_ALBUMS,),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_albums=[dst_album],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_saved_albums=True,
                write_saved_albums=True,
                search_albums=True,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertEqual(item.entity_type, EntityType.ALBUM)
        self.assertEqual(item.operation, TransferOperation.SAVE_ALBUM)
        self.assertEqual(item.source_id, "src-alb-1")
        self.assertEqual(item.destination_id, "dst-alb-99")
        self.assertEqual(item.match_method, MatchMethod.EXACT_METADATA)
        self.assertEqual(item.match_score, 1.0)
        self.assertEqual(item.status, ItemStatus.MATCHED)

    def test_cross_platform_artist_uses_search_when_id_not_reusable(self) -> None:
        """Cross-platform artist transfer without ID reuse searches destination and assigns resolved ID."""
        src_artist = artist("Radiohead", identifier="src-art-1")
        dst_artist = artist(
            "Radiohead", identifier="dst-art-99", platform=Platform.SPOTIFY
        )

        snap = snapshot(artists=(src_artist,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.FOLLOWED_ARTISTS,),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_artists=[dst_artist],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_followed_artists=True,
                write_followed_artists=True,
                search_artists=True,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertEqual(item.entity_type, EntityType.ARTIST)
        self.assertEqual(item.operation, TransferOperation.FOLLOW_ARTIST)
        self.assertEqual(item.source_id, "src-art-1")
        self.assertEqual(item.destination_id, "dst-art-99")
        self.assertEqual(item.match_method, MatchMethod.EXACT_METADATA)
        self.assertEqual(item.match_score, 1.0)
        self.assertEqual(item.status, ItemStatus.MATCHED)

    def test_album_search_no_candidates_is_not_found(self) -> None:
        """Cross-platform album search with 0 candidates is explicitly classified NOT_FOUND."""
        src_album = album("Kid A", identifier="src-alb-2", artists=["Radiohead"])
        snap = snapshot(albums=(src_album,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.SAVED_ALBUMS,),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_albums=[],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_saved_albums=True,
                write_saved_albums=True,
                search_albums=True,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertIsNone(item.destination_id)
        self.assertEqual(item.status, ItemStatus.NOT_FOUND)
        self.assertEqual(item.last_error, "not_found")
        self.assertFalse(item.is_executable())

    def test_artist_search_no_candidates_is_not_found(self) -> None:
        """Cross-platform artist search with 0 candidates is explicitly classified NOT_FOUND."""
        src_artist = artist("Thom Yorke", identifier="src-art-2")
        snap = snapshot(artists=(src_artist,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.FOLLOWED_ARTISTS,),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_artists=[],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_followed_artists=True,
                write_followed_artists=True,
                search_artists=True,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertIsNone(item.destination_id)
        self.assertEqual(item.status, ItemStatus.NOT_FOUND)
        self.assertEqual(item.last_error, "not_found")
        self.assertFalse(item.is_executable())

    def test_album_missing_search_capability_is_non_executable(self) -> None:
        """Destination lacking search_albums marks album item NOT_FOUND with destination_resolution_unavailable."""
        src_album = album("OK Computer", identifier="src-alb-1", artists=["Radiohead"])
        snap = snapshot(albums=(src_album,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.SAVED_ALBUMS,),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_saved_albums=True,
                write_saved_albums=True,
                search_albums=False,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertIsNone(item.destination_id)
        self.assertEqual(item.status, ItemStatus.NOT_FOUND)
        self.assertEqual(item.last_error, "destination_resolution_unavailable")
        self.assertFalse(item.is_executable())

    def test_artist_missing_search_capability_is_non_executable(self) -> None:
        """Destination lacking search_artists marks artist item NOT_FOUND with destination_resolution_unavailable."""
        src_artist = artist("Radiohead", identifier="src-art-1")
        snap = snapshot(artists=(src_artist,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.FOLLOWED_ARTISTS,),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_followed_artists=True,
                write_followed_artists=True,
                search_artists=False,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertIsNone(item.destination_id)
        self.assertEqual(item.status, ItemStatus.NOT_FOUND)
        self.assertEqual(item.last_error, "destination_resolution_unavailable")
        self.assertFalse(item.is_executable())

    def test_album_ambiguous_match_is_not_executable(self) -> None:
        """Ambiguous album match with near-score candidates is marked AMBIGUOUS with destination_id=None."""
        src_album = album("Greatest Hits", identifier="src-alb-1", artists=["Various"])
        cand1 = album(
            "Greatest Hits",
            identifier="dst-alb-1",
            platform=Platform.SPOTIFY,
            artists=["Various"],
        )
        cand2 = album(
            "Greatest Hits",
            identifier="dst-alb-2",
            platform=Platform.SPOTIFY,
            artists=["Various"],
        )

        snap = snapshot(albums=(src_album,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.SAVED_ALBUMS,),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_albums=[cand1, cand2],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_saved_albums=True,
                write_saved_albums=True,
                search_albums=True,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertIsNone(item.destination_id)
        self.assertEqual(item.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(item.last_error, "ambiguous")
        self.assertFalse(item.is_executable())

    def test_artist_ambiguous_match_is_not_executable(self) -> None:
        """Ambiguous artist match with multiple identical names is marked AMBIGUOUS with destination_id=None."""
        src_artist = artist("Aurora", identifier="src-art-1")
        cand1 = artist("AURORA", identifier="dst-art-1", platform=Platform.SPOTIFY)
        cand2 = artist("Aurora", identifier="dst-art-2", platform=Platform.SPOTIFY)

        snap = snapshot(artists=(src_artist,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.FOLLOWED_ARTISTS,),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_artists=[cand1, cand2],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_followed_artists=True,
                write_followed_artists=True,
                search_artists=True,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertIsNone(item.destination_id)
        self.assertEqual(item.status, ItemStatus.AMBIGUOUS)
        self.assertEqual(item.last_error, "ambiguous")
        self.assertFalse(item.is_executable())

    def test_presence_uses_resolved_destination_album_id(self) -> None:
        """Presence detection checks destination state using the resolved destination album ID."""
        src_album = album("OK Computer", identifier="src-alb-1", artists=["Radiohead"])
        dst_album = album(
            "OK Computer",
            identifier="dst-alb-99",
            platform=Platform.SPOTIFY,
            artists=["Radiohead"],
        )

        snap = snapshot(albums=(src_album,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.SAVED_ALBUMS,),
            settings=TransferSettings(skip_already_existing=True),
        )

        # Destination has dst-alb-99 in search catalog and already saved in library
        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_albums=[dst_album],
            albums=[dst_album],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_saved_albums=True,
                write_saved_albums=True,
                search_albums=True,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertEqual(item.destination_id, "dst-alb-99")
        self.assertEqual(item.status, ItemStatus.ALREADY_EXISTS)

    def test_presence_uses_resolved_destination_artist_id(self) -> None:
        """Presence detection checks destination state using the resolved destination artist ID."""
        src_artist = artist("Radiohead", identifier="src-art-1")
        dst_artist = artist(
            "Radiohead", identifier="dst-art-99", platform=Platform.SPOTIFY
        )

        snap = snapshot(artists=(src_artist,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.FOLLOWED_ARTISTS,),
            settings=TransferSettings(skip_already_existing=True),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_artists=[dst_artist],
            artists=[dst_artist],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_followed_artists=True,
                write_followed_artists=True,
                search_artists=True,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.items), 1)
        item = res.items[0]
        self.assertEqual(item.destination_id, "dst-art-99")
        self.assertEqual(item.status, ItemStatus.ALREADY_EXISTS)

    def test_album_absent_precondition_uses_resolved_destination_id(self) -> None:
        """Generated precondition for an absent matched album records resolved destination_id."""
        src_album = album("OK Computer", identifier="src-alb-1", artists=["Radiohead"])
        dst_album = album(
            "OK Computer",
            identifier="dst-alb-99",
            platform=Platform.SPOTIFY,
            artists=["Radiohead"],
        )

        snap = snapshot(albums=(src_album,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.SAVED_ALBUMS,),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_albums=[dst_album],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_saved_albums=True,
                write_saved_albums=True,
                search_albums=True,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.plan.preconditions), 1)
        precond = res.plan.preconditions[0]
        self.assertEqual(precond.entity_type, EntityType.ALBUM)
        self.assertEqual(precond.destination_id, "dst-alb-99")
        self.assertEqual(precond.expected, PreconditionExpectation.ABSENT)
        self.assertEqual(precond.section, "albums")

    def test_artist_absent_precondition_uses_resolved_destination_id(self) -> None:
        """Generated precondition for an absent matched artist records resolved destination_id."""
        src_artist = artist("Radiohead", identifier="src-art-1")
        dst_artist = artist(
            "Radiohead", identifier="dst-art-99", platform=Platform.SPOTIFY
        )

        snap = snapshot(artists=(src_artist,))
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            source_account_id="src-acc",
            destination_account_id="dst-acc",
            requested_content=(ContentType.FOLLOWED_ARTISTS,),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_artists=[dst_artist],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_followed_artists=True,
                write_followed_artists=True,
                search_artists=True,
                supports_already_exists_detection=True,
            ),
        )

        planner = TransferPlanner()
        res = planner.build(job, snap, ReadOnlyAdapter(dst))

        self.assertEqual(len(res.plan.preconditions), 1)
        precond = res.plan.preconditions[0]
        self.assertEqual(precond.entity_type, EntityType.ARTIST)
        self.assertEqual(precond.destination_id, "dst-art-99")
        self.assertEqual(precond.expected, PreconditionExpectation.ABSENT)
        self.assertEqual(precond.section, "artists")

    def test_executable_set_like_item_never_has_missing_destination_id(self) -> None:
        """Validation helper fails closed if any executable set-like item lacks a destination_id."""
        item = TransferItem.create(
            "job-1",
            EntityType.ALBUM,
            Platform.TIDAL,
            "src-alb-1",
            Platform.SPOTIFY,
            operation=TransferOperation.SAVE_ALBUM,
        )
        item.destination_id = None
        item.status = ItemStatus.MATCHED

        with self.assertRaises(TransferConfigurationError) as ctx:
            validate_plan_set_like_items([item])
        self.assertIn("unresolved_executable_item:album:src-alb-1", str(ctx.exception))

    def test_confirmed_plan_does_not_rematch_on_execution(self) -> None:
        """During write execution, executor uses plan destination_id without searching."""
        src_album = album("OK Computer", identifier="src-alb-1", artists=["Radiohead"])
        dst_album = album(
            "OK Computer",
            identifier="dst-alb-99",
            platform=Platform.SPOTIFY,
            artists=["Radiohead"],
        )

        snap = snapshot(albums=(src_album,))
        job = self.service.create_job(
            Platform.TIDAL,
            Platform.SPOTIFY,
            content=(ContentType.SAVED_ALBUMS,),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_albums=[dst_album],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_saved_albums=True,
                write_saved_albums=True,
                search_albums=True,
                supports_already_exists_detection=True,
            ),
        )

        src = FakePlatformAdapter(
            platform=Platform.TIDAL,
            capabilities=PlatformCapabilities(
                platform=Platform.TIDAL,
                read_saved_albums=True,
                read_followed_artists=True,
            ),
        )

        plan = self.service.analyze(job, src, dst, snapshot=snap)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Execute confirmed plan
        res = self.service.execute(job, dst, confirmed=True)
        self.assertEqual(res["outcome"].succeeded, 1)
        self.assertEqual(res["report"].transferred, 1)
        self.assertEqual(dst.saved_albums, ["dst-alb-99"])

    def test_confirmed_plan_does_not_rematch_on_resume(self) -> None:
        """Resuming execution uses existing plan destination_id without rematching."""
        src_artist = artist("Radiohead", identifier="src-art-1")
        dst_artist = artist(
            "Radiohead", identifier="dst-art-99", platform=Platform.SPOTIFY
        )

        snap = snapshot(artists=(src_artist,))
        job = self.service.create_job(
            Platform.TIDAL,
            Platform.SPOTIFY,
            content=(ContentType.FOLLOWED_ARTISTS,),
        )

        src = FakePlatformAdapter(
            platform=Platform.TIDAL,
            capabilities=PlatformCapabilities(
                platform=Platform.TIDAL,
                read_saved_albums=True,
                read_followed_artists=True,
            ),
        )

        dst = FakePlatformAdapter(
            platform=Platform.SPOTIFY,
            catalog_artists=[dst_artist],
            capabilities=PlatformCapabilities(
                platform=Platform.SPOTIFY,
                read_followed_artists=True,
                write_followed_artists=True,
                search_artists=True,
                supports_already_exists_detection=True,
            ),
        )

        plan = self.service.analyze(job, src, dst, snapshot=snap)
        self.service.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )

        # Execute confirmed plan
        res = self.service.execute(job, dst, confirmed=True)
        self.assertEqual(res["outcome"].succeeded, 1)
        self.assertEqual(res["report"].transferred, 1)
        self.assertEqual(dst.followed_artists, ["dst-art-99"])

        # Check persisted item status
        items = self.service.items.list_for_job(job.id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].status, ItemStatus.TRANSFERRED)
        self.assertEqual(items[0].destination_id, "dst-art-99")

    def test_album_matcher_upc_and_qualifier_matching(self) -> None:
        """AlbumMatcher correctly matches by UPC, exact title, and normalized base title."""
        policy = MatchingPolicy()
        matcher = AlbumMatcher(policy)

        # 1. UPC match
        src = album(
            "Abbey Road",
            identifier="src-1",
            upc="0077774644624",
            artists=["The Beatles"],
        )
        cand = album(
            "Abbey Road (Super Deluxe Edition)",
            identifier="dst-1",
            upc="0077774644624",
            artists=["The Beatles"],
        )
        res = matcher.match(src, [cand])
        self.assertEqual(res.outcome, MatchOutcome.MATCHED)
        self.assertEqual(res.destination_id, "dst-1")
        self.assertEqual(res.method, MatchMethod.EXACT_METADATA)
        self.assertIn("upc_equal", res.reasons)

        # 2. Qualifier stripped match
        src_remaster = album(
            "Abbey Road (Remastered 2009)",
            identifier="src-2",
            artists=["The Beatles"],
        )
        cand_base = album(
            "Abbey Road",
            identifier="dst-2",
            artists=["The Beatles"],
        )
        res_base = matcher.match(src_remaster, [cand_base])
        self.assertEqual(res_base.outcome, MatchOutcome.MATCHED)
        self.assertEqual(res_base.destination_id, "dst-2")
        self.assertAlmostEqual(res_base.score, 0.90)

    def test_artist_matcher_exact_and_ambiguity(self) -> None:
        """ArtistMatcher matches exact case/diacritics and disambiguates collisions."""
        policy = MatchingPolicy()
        matcher = ArtistMatcher(policy)

        # Diacritic normalization
        src = artist("Björk", identifier="src-1")
        cand = artist("Bjork", identifier="dst-1")
        res = matcher.match(src, [cand])
        self.assertEqual(res.outcome, MatchOutcome.MATCHED)
        self.assertEqual(res.destination_id, "dst-1")
        self.assertEqual(res.method, MatchMethod.EXACT_METADATA)

        # Name collision
        cand2 = artist("BJORK", identifier="dst-2")
        res_amb = matcher.match(src, [cand, cand2])
        self.assertEqual(res_amb.outcome, MatchOutcome.AMBIGUOUS)
        self.assertIsNone(res_amb.destination_id)


if __name__ == "__main__":
    unittest.main()

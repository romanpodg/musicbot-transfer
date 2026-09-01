"""Comprehensive unit and regression tests for Phase 1.5B: VIDEOS and MIXES End-to-End Transfer Support.

Validates:
1. ENGINE_TRANSFER_SPECS registration for ContentType.VIDEOS and ContentType.MIXES.
2. REUSE_ONLY resolution policy (never search, search_capability=None).
3. Capability validation (read_videos/write_videos, read_mixes/write_mixes).
4. Selective source export (reading only requested sections).
5. DestinationState tri-state presence (PRESENT, ABSENT, UNKNOWN) for videos and mixes.
6. destination_section_for_entity and identifiers_for_section mappings.
7. TidalAdapter get_destination_state selective section scoping for videos and mixes.
8. Same-platform TIDAL direct ID reuse vs cross-platform destination_resolution_unavailable.
9. Already exists skipping and exact precondition construction.
10. TransferItem.is_executable and plan validation for SAVE_VIDEO and SAVE_MIX.
11. TransferExecutor dispatch calling save_video and save_mix.
12. Precondition preflight revalidation (stale precondition blocks writes).
13. TransferVerifier verification of videos and mixes sections.
14. RecoveryService ambiguous write reconciliation using destination presence.
15. End-to-end TIDAL transfers for videos and mixes.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from music_transfer.app.services import TransferService
from music_transfer.core.domain import (
    Account,
    AccountProfile,
    LibrarySnapshot,
    TransferItem,
    TransferJob,
)
from music_transfer.core.enums import (
    ContentType,
    DestinationPresence,
    EntityType,
    IdentifierResolutionPolicy,
    ItemStatus,
    JobStatus,
    MatchMethod,
    MutationState,
    Platform,
    PreconditionExpectation,
    TransferOperation,
)
from music_transfer.core.errors import (
    InvalidDestinationSectionError,
    PlanStaleError,
    TransferConfigurationError,
    UnsupportedTransferContentError,
)
from music_transfer.core.ports import (
    DestinationState,
    PlatformCapabilities,
    ReadOnlyAdapter,
    destination_section_for_entity,
)
from music_transfer.core.transfer import (
    ENGINE_TRANSFER_SPECS,
    RecoveryService,
    TransferExecutor,
    TransferPlanner,
    TransferVerifier,
    content_sections,
    require_transfer_content_spec,
    validate_plan_set_like_items,
    validate_transfer_content_support,
)
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
    JsonTransferPlanRepository,
)
from music_transfer.platforms.tidal import TidalAdapter

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
    mutation_state: MutationState = MutationState.NONE,
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
        mutation_state=mutation_state,
    )


class VideoMixTransferSpecTests(unittest.TestCase):
    """Specification and capability tests for VIDEOS and MIXES."""

    def test_video_transfer_spec_is_engine_supported(self) -> None:
        """ContentType.VIDEOS is registered in ENGINE_TRANSFER_SPECS with complete transfer path."""
        spec = require_transfer_content_spec(ContentType.VIDEOS)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.content_type, ContentType.VIDEOS)
        self.assertEqual(spec.entity_type, EntityType.VIDEO)
        self.assertEqual(spec.operation, TransferOperation.SAVE_VIDEO)
        self.assertEqual(spec.snapshot_sections, ("videos",))
        self.assertEqual(spec.source_read_capabilities, ("read_videos",))
        self.assertEqual(spec.destination_write_capabilities, ("write_videos",))

    def test_mix_transfer_spec_is_engine_supported(self) -> None:
        """ContentType.MIXES is registered in ENGINE_TRANSFER_SPECS with complete transfer path."""
        spec = require_transfer_content_spec(ContentType.MIXES)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.content_type, ContentType.MIXES)
        self.assertEqual(spec.entity_type, EntityType.MIX)
        self.assertEqual(spec.operation, TransferOperation.SAVE_MIX)
        self.assertEqual(spec.snapshot_sections, ("mixes",))
        self.assertEqual(spec.source_read_capabilities, ("read_mixes",))
        self.assertEqual(spec.destination_write_capabilities, ("write_mixes",))

    def test_video_spec_uses_reuse_only(self) -> None:
        """Video transfer uses IdentifierResolutionPolicy.REUSE_ONLY without catalog search."""
        spec = ENGINE_TRANSFER_SPECS[ContentType.VIDEOS]
        self.assertEqual(spec.resolution_policy, IdentifierResolutionPolicy.REUSE_ONLY)
        self.assertIsNone(spec.search_capability)

    def test_mix_spec_uses_reuse_only(self) -> None:
        """Mix transfer uses IdentifierResolutionPolicy.REUSE_ONLY without catalog search."""
        spec = ENGINE_TRANSFER_SPECS[ContentType.MIXES]
        self.assertEqual(spec.resolution_policy, IdentifierResolutionPolicy.REUSE_ONLY)
        self.assertIsNone(spec.search_capability)

    def test_video_source_capability_required(self) -> None:
        """Video transfer requires source read_videos capability."""
        source_cap = PlatformCapabilities(platform=Platform.TIDAL, read_videos=False)
        dest_cap = PlatformCapabilities(platform=Platform.TIDAL, write_videos=True)
        with self.assertRaises(UnsupportedTransferContentError) as ctx:
            validate_transfer_content_support((ContentType.VIDEOS,), source_cap, dest_cap)
        self.assertEqual(ctx.exception.code, "unsupported_transfer_content")
        self.assertEqual(ctx.exception.reason, "source_read_unsupported")
        self.assertEqual(ctx.exception.content_type, ContentType.VIDEOS)
        self.assertEqual(ctx.exception.capability, "read_videos")

    def test_video_destination_capability_required(self) -> None:
        """Video transfer requires destination write_videos capability."""
        source_cap = PlatformCapabilities(platform=Platform.TIDAL, read_videos=True)
        dest_cap = PlatformCapabilities(platform=Platform.TIDAL, write_videos=False)
        with self.assertRaises(UnsupportedTransferContentError) as ctx:
            validate_transfer_content_support((ContentType.VIDEOS,), source_cap, dest_cap)
        self.assertEqual(ctx.exception.code, "unsupported_transfer_content")
        self.assertEqual(ctx.exception.reason, "destination_write_unsupported")
        self.assertEqual(ctx.exception.content_type, ContentType.VIDEOS)
        self.assertEqual(ctx.exception.capability, "write_videos")

    def test_mix_source_capability_required(self) -> None:
        """Mix transfer requires source read_mixes capability."""
        source_cap = PlatformCapabilities(platform=Platform.TIDAL, read_mixes=False)
        dest_cap = PlatformCapabilities(platform=Platform.TIDAL, write_mixes=True)
        with self.assertRaises(UnsupportedTransferContentError) as ctx:
            validate_transfer_content_support((ContentType.MIXES,), source_cap, dest_cap)
        self.assertEqual(ctx.exception.code, "unsupported_transfer_content")
        self.assertEqual(ctx.exception.reason, "source_read_unsupported")
        self.assertEqual(ctx.exception.content_type, ContentType.MIXES)
        self.assertEqual(ctx.exception.capability, "read_mixes")

    def test_mix_destination_capability_required(self) -> None:
        """Mix transfer requires destination write_mixes capability."""
        source_cap = PlatformCapabilities(platform=Platform.TIDAL, read_mixes=True)
        dest_cap = PlatformCapabilities(platform=Platform.TIDAL, write_mixes=False)
        with self.assertRaises(UnsupportedTransferContentError) as ctx:
            validate_transfer_content_support((ContentType.MIXES,), source_cap, dest_cap)
        self.assertEqual(ctx.exception.code, "unsupported_transfer_content")
        self.assertEqual(ctx.exception.reason, "destination_write_unsupported")
        self.assertEqual(ctx.exception.content_type, ContentType.MIXES)
        self.assertEqual(ctx.exception.capability, "write_mixes")


class VideoMixSelectiveExportTests(unittest.TestCase):
    """Selective source export tests for VIDEOS and MIXES."""

    def test_video_only_source_export_reads_only_videos(self) -> None:
        """Requesting only VIDEOS resolves snapshot_sections to ('videos',)."""
        sections = content_sections((ContentType.VIDEOS,))
        self.assertEqual(sections, ("videos",))

    def test_mix_only_source_export_reads_only_mixes(self) -> None:
        """Requesting only MIXES resolves snapshot_sections to ('mixes',)."""
        sections = content_sections((ContentType.MIXES,))
        self.assertEqual(sections, ("mixes",))

    def test_video_mix_export_reads_exact_union(self) -> None:
        """Requesting VIDEOS and MIXES resolves to exact union ('mixes', 'videos')."""
        sections = content_sections((ContentType.VIDEOS, ContentType.MIXES))
        self.assertEqual(set(sections), {"videos", "mixes"})


class VideoMixDestinationPresenceTests(unittest.TestCase):
    """Destination presence semantics for videos and mixes."""

    def test_destination_presence_video_present_absent_unknown(self) -> None:
        """Video presence returns PRESENT when observed in complete section, ABSENT when missing, UNKNOWN when incomplete/unread."""
        complete_state = DestinationState(
            platform=Platform.TIDAL,
            video_ids=frozenset({"vid-1"}),
            complete_sections=frozenset({"videos"}),
        )
        self.assertEqual(complete_state.presence(EntityType.VIDEO, "vid-1"), DestinationPresence.PRESENT)
        self.assertEqual(complete_state.presence(EntityType.VIDEO, "vid-2"), DestinationPresence.ABSENT)
        self.assertEqual(complete_state.presence_in_section("videos", "vid-1"), DestinationPresence.PRESENT)
        self.assertEqual(complete_state.presence_in_section("videos", "vid-2"), DestinationPresence.ABSENT)

        unread_state = DestinationState(platform=Platform.TIDAL)
        self.assertEqual(unread_state.presence(EntityType.VIDEO, "vid-1"), DestinationPresence.UNKNOWN)

        incomplete_state = DestinationState(platform=Platform.TIDAL, incomplete_sections=("videos",))
        self.assertEqual(incomplete_state.presence(EntityType.VIDEO, "vid-1"), DestinationPresence.UNKNOWN)

    def test_destination_presence_mix_present_absent_unknown(self) -> None:
        """Mix presence returns PRESENT when observed in complete section, ABSENT when missing, UNKNOWN when incomplete/unread."""
        complete_state = DestinationState(
            platform=Platform.TIDAL,
            mix_ids=frozenset({"mix-1"}),
            complete_sections=frozenset({"mixes"}),
        )
        self.assertEqual(complete_state.presence(EntityType.MIX, "mix-1"), DestinationPresence.PRESENT)
        self.assertEqual(complete_state.presence(EntityType.MIX, "mix-2"), DestinationPresence.ABSENT)
        self.assertEqual(complete_state.presence_in_section("mixes", "mix-1"), DestinationPresence.PRESENT)
        self.assertEqual(complete_state.presence_in_section("mixes", "mix-2"), DestinationPresence.ABSENT)

        unread_state = DestinationState(platform=Platform.TIDAL)
        self.assertEqual(unread_state.presence(EntityType.MIX, "mix-1"), DestinationPresence.UNKNOWN)

        incomplete_state = DestinationState(platform=Platform.TIDAL, incomplete_sections=("mixes",))
        self.assertEqual(incomplete_state.presence(EntityType.MIX, "mix-1"), DestinationPresence.UNKNOWN)

    def test_destination_section_for_entity(self) -> None:
        """destination_section_for_entity maps VIDEO to 'videos' and MIX to 'mixes'."""
        self.assertEqual(destination_section_for_entity(EntityType.VIDEO), "videos")
        self.assertEqual(destination_section_for_entity(EntityType.MIX), "mixes")
        self.assertEqual(destination_section_for_entity(EntityType.TRACK), "tracks")
        self.assertEqual(destination_section_for_entity(EntityType.ALBUM), "albums")
        self.assertEqual(destination_section_for_entity(EntityType.ARTIST), "artists")
        self.assertEqual(destination_section_for_entity(EntityType.PLAYLIST), "playlists")
        with self.assertRaises(InvalidDestinationSectionError):
            destination_section_for_entity(EntityType.FOLDER)

    def test_identifiers_for_section(self) -> None:
        """identifiers_for_section returns the corresponding ID frozenset."""
        state = DestinationState(
            platform=Platform.TIDAL,
            video_ids=frozenset({"v1", "v2"}),
            mix_ids=frozenset({"m1"}),
            complete_sections=frozenset({"videos", "mixes"}),
        )
        self.assertEqual(state.identifiers_for_section("videos"), frozenset({"v1", "v2"}))
        self.assertEqual(state.identifiers_for_section("mixes"), frozenset({"m1"}))
        with self.assertRaises(InvalidDestinationSectionError):
            state.identifiers_for_section("invalid_section")


class VideoMixTidalAdapterTests(unittest.TestCase):
    """TidalAdapter selective destination state reads for videos and mixes."""

    class MockTidalClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def liked_tracks(self, progress: Any = None) -> list[Any]:
            self.calls.append("liked_tracks")
            return []

        def saved_albums(self, progress: Any = None) -> list[Any]:
            self.calls.append("saved_albums")
            return []

        def followed_artists(self, progress: Any = None) -> list[Any]:
            self.calls.append("followed_artists")
            return []

        def playlists(self, progress: Any = None) -> list[Any]:
            self.calls.append("playlists")
            return []

        def videos(self, progress: Any = None) -> list[Any]:
            self.calls.append("videos")
            return [record("vid-100", "Official Video")]

        def mixes(self, progress: Any = None) -> list[Any]:
            self.calls.append("mixes")
            return [record("mix-200", "My Daily Mix")]

        def profile(self) -> Any:
            return None

    def test_tidal_destination_state_videos_only(self) -> None:
        """sections=('videos',) reads only the videos endpoint."""
        client = self.MockTidalClient()
        adapter = TidalAdapter(client)
        state = adapter.get_destination_state(sections=("videos",))
        self.assertEqual(client.calls, ["videos"])
        self.assertEqual(state.complete_sections, frozenset({"videos"}))
        self.assertEqual(state.video_ids, frozenset({"vid-100"}))
        self.assertEqual(state.presence(EntityType.VIDEO, "vid-100"), DestinationPresence.PRESENT)
        self.assertEqual(state.presence(EntityType.VIDEO, "vid-999"), DestinationPresence.ABSENT)
        self.assertEqual(state.presence(EntityType.MIX, "mix-200"), DestinationPresence.UNKNOWN)

    def test_tidal_destination_state_mixes_only(self) -> None:
        """sections=('mixes',) reads only the mixes endpoint."""
        client = self.MockTidalClient()
        adapter = TidalAdapter(client)
        state = adapter.get_destination_state(sections=("mixes",))
        self.assertEqual(client.calls, ["mixes"])
        self.assertEqual(state.complete_sections, frozenset({"mixes"}))
        self.assertEqual(state.mix_ids, frozenset({"mix-200"}))
        self.assertEqual(state.presence(EntityType.MIX, "mix-200"), DestinationPresence.PRESENT)
        self.assertEqual(state.presence(EntityType.MIX, "mix-999"), DestinationPresence.ABSENT)
        self.assertEqual(state.presence(EntityType.VIDEO, "vid-100"), DestinationPresence.UNKNOWN)


class VideoMixPlanningAndResolutionTests(unittest.TestCase):
    """Identifier resolution and planning tests for VIDEOS and MIXES."""

    def test_tidal_video_direct_id_reuse(self) -> None:
        """Same-platform TIDAL video transfer directly reuses the source identifier."""
        source_video = record("vid-1", "Video 1")
        snapshot = LibrarySnapshot(
            account=AccountProfile("1", "src", Platform.TIDAL),
            platform=Platform.TIDAL,
            videos=[source_video],
        )
        planner = TransferPlanner()
        dest_adapter = FakePlatformAdapter(platform=Platform.TIDAL)
        read_port = ReadOnlyAdapter(dest_adapter)
        state = DestinationState(platform=Platform.TIDAL, complete_sections=frozenset({"videos"}))

        job = TransferJob(
            id="job-1",
            user_id="user-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.VIDEOS,),
        )
        result = planner.build(job, snapshot, read_port, destination_state=state)
        items = result.items

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.entity_type, EntityType.VIDEO)
        self.assertEqual(item.source_id, "vid-1")
        self.assertEqual(item.destination_id, "vid-1")
        self.assertEqual(item.status, ItemStatus.MATCHED)
        self.assertEqual(item.match_method, MatchMethod.DIRECT_ID)
        self.assertEqual(item.operation, TransferOperation.SAVE_VIDEO)
        self.assertTrue(item.is_executable())

    def test_tidal_mix_direct_id_reuse(self) -> None:
        """Same-platform TIDAL mix transfer directly reuses the source identifier."""
        source_mix = record("mix-1", "Mix 1")
        snapshot = LibrarySnapshot(
            account=AccountProfile("1", "src", Platform.TIDAL),
            platform=Platform.TIDAL,
            mixes=[source_mix],
        )
        planner = TransferPlanner()
        dest_adapter = FakePlatformAdapter(platform=Platform.TIDAL)
        read_port = ReadOnlyAdapter(dest_adapter)
        state = DestinationState(platform=Platform.TIDAL, complete_sections=frozenset({"mixes"}))

        job = TransferJob(
            id="job-1",
            user_id="user-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.MIXES,),
        )
        result = planner.build(job, snapshot, read_port, destination_state=state)
        items = result.items

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.entity_type, EntityType.MIX)
        self.assertEqual(item.source_id, "mix-1")
        self.assertEqual(item.destination_id, "mix-1")
        self.assertEqual(item.status, ItemStatus.MATCHED)
        self.assertEqual(item.match_method, MatchMethod.DIRECT_ID)
        self.assertEqual(item.operation, TransferOperation.SAVE_MIX)
        self.assertTrue(item.is_executable())

    def test_cross_platform_video_without_reusable_id_is_not_found(self) -> None:
        """Cross-platform video without reusable ID resolves to NOT_FOUND without searching."""
        class NonReusingDestination(FakePlatformAdapter):
            def can_reuse_identifier(self, entity_type: EntityType, source: Platform) -> bool:
                return False

        source_video = record("vid-1", "Video 1", platform=Platform.SPOTIFY)
        snapshot = LibrarySnapshot(
            account=AccountProfile("1", "src", Platform.SPOTIFY),
            platform=Platform.SPOTIFY,
            videos=[source_video],
        )
        planner = TransferPlanner()
        dest_adapter = NonReusingDestination(platform=Platform.TIDAL)
        read_port = ReadOnlyAdapter(dest_adapter)
        state = DestinationState(platform=Platform.TIDAL, complete_sections=frozenset({"videos"}))

        job = TransferJob(
            id="job-1",
            user_id="user-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.SPOTIFY,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.VIDEOS,),
        )
        result = planner.build(job, snapshot, read_port, destination_state=state)
        items = result.items

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.entity_type, EntityType.VIDEO)
        self.assertEqual(item.status, ItemStatus.NOT_FOUND)
        self.assertIsNone(item.destination_id)
        self.assertEqual(item.last_error, "destination_resolution_unavailable")
        self.assertFalse(item.is_executable())

    def test_cross_platform_mix_without_reusable_id_is_not_found(self) -> None:
        """Cross-platform mix without reusable ID resolves to NOT_FOUND without searching."""
        class NonReusingDestination(FakePlatformAdapter):
            def can_reuse_identifier(self, entity_type: EntityType, source: Platform) -> bool:
                return False

        source_mix = record("mix-1", "Mix 1", platform=Platform.SPOTIFY)
        snapshot = LibrarySnapshot(
            account=AccountProfile("1", "src", Platform.SPOTIFY),
            platform=Platform.SPOTIFY,
            mixes=[source_mix],
        )
        planner = TransferPlanner()
        dest_adapter = NonReusingDestination(platform=Platform.TIDAL)
        read_port = ReadOnlyAdapter(dest_adapter)
        state = DestinationState(platform=Platform.TIDAL, complete_sections=frozenset({"mixes"}))

        job = TransferJob(
            id="job-1",
            user_id="user-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.SPOTIFY,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.MIXES,),
        )
        result = planner.build(job, snapshot, read_port, destination_state=state)
        items = result.items

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.entity_type, EntityType.MIX)
        self.assertEqual(item.status, ItemStatus.NOT_FOUND)
        self.assertIsNone(item.destination_id)
        self.assertEqual(item.last_error, "destination_resolution_unavailable")
        self.assertFalse(item.is_executable())

    def test_video_already_exists_is_skipped(self) -> None:
        """When destination already contains video ID, item is marked ALREADY_EXISTS with PRESENT precondition."""
        source_video = record("vid-1", "Video 1")
        snapshot = LibrarySnapshot(
            account=AccountProfile("1", "src", Platform.TIDAL),
            platform=Platform.TIDAL,
            videos=[source_video],
        )
        planner = TransferPlanner()
        dest_adapter = FakePlatformAdapter(platform=Platform.TIDAL)
        read_port = ReadOnlyAdapter(dest_adapter)
        state = DestinationState(
            platform=Platform.TIDAL,
            video_ids=frozenset({"vid-1"}),
            complete_sections=frozenset({"videos"}),
        )

        job = TransferJob(
            id="job-1",
            user_id="user-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.VIDEOS,),
        )
        result = planner.build(job, snapshot, read_port, destination_state=state)
        plan = result.plan
        items = result.items

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.status, ItemStatus.ALREADY_EXISTS)
        self.assertFalse(item.is_executable())

        # Precondition check
        self.assertEqual(len(plan.preconditions), 1)
        prec = plan.preconditions[0]
        self.assertEqual(prec.entity_type, EntityType.VIDEO)
        self.assertEqual(prec.destination_id, "vid-1")
        self.assertEqual(prec.expected, PreconditionExpectation.PRESENT)
        self.assertEqual(prec.section, "videos")

    def test_mix_already_exists_is_skipped(self) -> None:
        """When destination already contains mix ID, item is marked ALREADY_EXISTS with PRESENT precondition."""
        source_mix = record("mix-1", "Mix 1")
        snapshot = LibrarySnapshot(
            account=AccountProfile("1", "src", Platform.TIDAL),
            platform=Platform.TIDAL,
            mixes=[source_mix],
        )
        planner = TransferPlanner()
        dest_adapter = FakePlatformAdapter(platform=Platform.TIDAL)
        read_port = ReadOnlyAdapter(dest_adapter)
        state = DestinationState(
            platform=Platform.TIDAL,
            mix_ids=frozenset({"mix-1"}),
            complete_sections=frozenset({"mixes"}),
        )

        job = TransferJob(
            id="job-1",
            user_id="user-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.MIXES,),
        )
        result = planner.build(job, snapshot, read_port, destination_state=state)
        plan = result.plan
        items = result.items

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.status, ItemStatus.ALREADY_EXISTS)
        self.assertFalse(item.is_executable())

        # Precondition check
        self.assertEqual(len(plan.preconditions), 1)
        prec = plan.preconditions[0]
        self.assertEqual(prec.entity_type, EntityType.MIX)
        self.assertEqual(prec.destination_id, "mix-1")
        self.assertEqual(prec.expected, PreconditionExpectation.PRESENT)
        self.assertEqual(prec.section, "mixes")

    def test_video_absent_precondition(self) -> None:
        """When destination state is complete and lacks video ID, item receives ABSENT precondition."""
        source_video = record("vid-1", "Video 1")
        snapshot = LibrarySnapshot(
            account=AccountProfile("1", "src", Platform.TIDAL),
            platform=Platform.TIDAL,
            videos=[source_video],
        )
        planner = TransferPlanner()
        dest_adapter = FakePlatformAdapter(platform=Platform.TIDAL)
        read_port = ReadOnlyAdapter(dest_adapter)
        state = DestinationState(
            platform=Platform.TIDAL,
            video_ids=frozenset(),
            complete_sections=frozenset({"videos"}),
        )

        job = TransferJob(
            id="job-1",
            user_id="user-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.VIDEOS,),
        )
        result = planner.build(job, snapshot, read_port, destination_state=state)
        plan = result.plan

        self.assertEqual(len(plan.preconditions), 1)
        prec = plan.preconditions[0]
        self.assertEqual(prec.entity_type, EntityType.VIDEO)
        self.assertEqual(prec.destination_id, "vid-1")
        self.assertEqual(prec.expected, PreconditionExpectation.ABSENT)
        self.assertEqual(prec.section, "videos")

    def test_mix_absent_precondition(self) -> None:
        """When destination state is complete and lacks mix ID, item receives ABSENT precondition."""
        source_mix = record("mix-1", "Mix 1")
        snapshot = LibrarySnapshot(
            account=AccountProfile("1", "src", Platform.TIDAL),
            platform=Platform.TIDAL,
            mixes=[source_mix],
        )
        planner = TransferPlanner()
        dest_adapter = FakePlatformAdapter(platform=Platform.TIDAL)
        read_port = ReadOnlyAdapter(dest_adapter)
        state = DestinationState(
            platform=Platform.TIDAL,
            mix_ids=frozenset(),
            complete_sections=frozenset({"mixes"}),
        )

        job = TransferJob(
            id="job-1",
            user_id="user-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.MIXES,),
        )
        result = planner.build(job, snapshot, read_port, destination_state=state)
        plan = result.plan

        self.assertEqual(len(plan.preconditions), 1)
        prec = plan.preconditions[0]
        self.assertEqual(prec.entity_type, EntityType.MIX)
        self.assertEqual(prec.destination_id, "mix-1")
        self.assertEqual(prec.expected, PreconditionExpectation.ABSENT)
        self.assertEqual(prec.section, "mixes")

    def test_video_executable_requires_destination_id(self) -> None:
        """SAVE_VIDEO item is executable only with non-empty destination_id and valid status."""
        item_executable = _make_item(
            item_id="item-1",
            job_id="job-1",
            entity_type=EntityType.VIDEO,
            source_id="vid-1",
            destination_id="vid-1",
            status=ItemStatus.MATCHED,
            operation=TransferOperation.SAVE_VIDEO,
        )
        self.assertTrue(item_executable.is_executable())

        item_unresolved = _make_item(
            item_id="item-2",
            job_id="job-1",
            entity_type=EntityType.VIDEO,
            source_id="vid-1",
            destination_id=None,
            status=ItemStatus.MATCHED,
            operation=TransferOperation.SAVE_VIDEO,
        )
        self.assertFalse(item_unresolved.is_executable())

        item_skipped = _make_item(
            item_id="item-3",
            job_id="job-1",
            entity_type=EntityType.VIDEO,
            source_id="vid-1",
            destination_id="vid-1",
            status=ItemStatus.ALREADY_EXISTS,
            operation=TransferOperation.SAVE_VIDEO,
        )
        self.assertFalse(item_skipped.is_executable())

    def test_mix_executable_requires_destination_id(self) -> None:
        """SAVE_MIX item is executable only with non-empty destination_id and valid status."""
        item_executable = _make_item(
            item_id="item-1",
            job_id="job-1",
            entity_type=EntityType.MIX,
            source_id="mix-1",
            destination_id="mix-1",
            status=ItemStatus.MATCHED,
            operation=TransferOperation.SAVE_MIX,
        )
        self.assertTrue(item_executable.is_executable())

        item_unresolved = _make_item(
            item_id="item-2",
            job_id="job-1",
            entity_type=EntityType.MIX,
            source_id="mix-1",
            destination_id=None,
            status=ItemStatus.MATCHED,
            operation=TransferOperation.SAVE_MIX,
        )
        self.assertFalse(item_unresolved.is_executable())

    def test_validate_plan_set_like_items_rejects_unresolved_video_and_mix(self) -> None:
        """validate_plan_set_like_items raises TransferConfigurationError if SAVE_VIDEO/SAVE_MIX has no destination_id."""
        item_bad_vid = _make_item(
            item_id="item-1",
            job_id="job-1",
            entity_type=EntityType.VIDEO,
            source_id="vid-1",
            destination_id=None,
            status=ItemStatus.MATCHED,
            operation=TransferOperation.SAVE_VIDEO,
        )
        with self.assertRaises(TransferConfigurationError):
            validate_plan_set_like_items([item_bad_vid])

        item_bad_mix = _make_item(
            item_id="item-2",
            job_id="job-1",
            entity_type=EntityType.MIX,
            source_id="mix-1",
            destination_id=None,
            status=ItemStatus.MATCHED,
            operation=TransferOperation.SAVE_MIX,
        )
        with self.assertRaises(TransferConfigurationError):
            validate_plan_set_like_items([item_bad_mix])


class VideoMixExecutionTests(unittest.TestCase):
    """Executor and preflight tests for VIDEOS and MIXES."""

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

    def test_executor_save_video(self) -> None:
        """TransferExecutor dispatches SAVE_VIDEO to destination.save_video and marks item TRANSFERRED."""
        dest = FakePlatformAdapter()
        root = Path(tempfile.mkdtemp())
        item_repo = JsonTransferItemRepository(root)
        executor = TransferExecutor(dest, item_repo)

        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.VIDEOS,),
        )
        item = _make_item(
            item_id="item-1",
            job_id="job-1",
            entity_type=EntityType.VIDEO,
            source_id="vid-100",
            destination_id="vid-100",
            status=ItemStatus.MATCHED,
            operation=TransferOperation.SAVE_VIDEO,
        )
        item_repo.add_many([item])

        executor.execute(job, [item])

        self.assertIn("vid-100", dest.saved_videos)
        self.assertIn(("save_video", ("vid-100",)), dest.write_calls)
        persisted = item_repo.load("job-1")
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].status, ItemStatus.TRANSFERRED)

    def test_executor_save_mix(self) -> None:
        """TransferExecutor dispatches SAVE_MIX to destination.save_mix and marks item TRANSFERRED."""
        dest = FakePlatformAdapter()
        root = Path(tempfile.mkdtemp())
        item_repo = JsonTransferItemRepository(root)
        executor = TransferExecutor(dest, item_repo)

        job = TransferJob(
            id="job-1",
            user_id="u-1",
            source_account_id="src-1",
            destination_account_id="dst-1",
            source_platform=Platform.TIDAL,
            destination_platform=Platform.TIDAL,
            requested_content=(ContentType.MIXES,),
        )
        item = _make_item(
            item_id="item-1",
            job_id="job-1",
            entity_type=EntityType.MIX,
            source_id="mix-200",
            destination_id="mix-200",
            status=ItemStatus.MATCHED,
            operation=TransferOperation.SAVE_MIX,
        )
        item_repo.add_many([item])

        executor.execute(job, [item])

        self.assertIn("mix-200", dest.saved_mixes)
        self.assertIn(("save_mix", ("mix-200",)), dest.write_calls)
        persisted = item_repo.load("job-1")
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].status, ItemStatus.TRANSFERRED)

    def test_video_stale_precondition_blocks_write(self) -> None:
        """If destination state changes so video is PRESENT when ABSENT was expected, execution fails with PlanStaleError."""
        service, _ = self._setup_service()
        src_account = _make_account("src-1")
        dst_account = _make_account("dst-1")

        source = FakePlatformAdapter(videos=[record("v1", "Video 1")])
        destination = FakePlatformAdapter()

        job = service.create_job(src_account, dst_account, content=(ContentType.VIDEOS,))
        plan = service.analyze(job, source, destination)
        service.confirm_plan(job, plan_id=plan.plan_id, revision=plan.revision, plan_hash=plan.plan_hash)

        # Dest mutates out-of-band before execution
        destination.saved_videos.append("v1")

        with self.assertRaises(PlanStaleError):
            service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        persisted_job = service._jobs.get(job.id)
        self.assertIsNotNone(persisted_job)
        self.assertIsNone(persisted_job.confirmed_at)
        self.assertEqual(persisted_job.status, JobStatus.WAITING_CONFIRMATION)

    def test_mix_stale_precondition_blocks_write(self) -> None:
        """If destination state changes so mix is PRESENT when ABSENT was expected, execution fails with PlanStaleError."""
        service, _ = self._setup_service()
        src_account = _make_account("src-1")
        dst_account = _make_account("dst-1")

        source = FakePlatformAdapter(mixes=[record("m1", "Mix 1")])
        destination = FakePlatformAdapter()

        job = service.create_job(src_account, dst_account, content=(ContentType.MIXES,))
        plan = service.analyze(job, source, destination)
        service.confirm_plan(job, plan_id=plan.plan_id, revision=plan.revision, plan_hash=plan.plan_hash)

        # Dest mutates out-of-band before execution
        destination.saved_mixes.append("m1")

        with self.assertRaises(PlanStaleError):
            service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        persisted_job = service._jobs.get(job.id)
        self.assertIsNotNone(persisted_job)
        self.assertIsNone(persisted_job.confirmed_at)
        self.assertEqual(persisted_job.status, JobStatus.WAITING_CONFIRMATION)


class VideoMixVerificationTests(unittest.TestCase):
    """TransferVerifier tests for videos and mixes."""

    def test_verify_videos(self) -> None:
        """TransferVerifier.verify_job includes 'videos' section and verifies IDs against destination."""
        dest = FakePlatformAdapter(videos=[record("v1", "V1"), record("v2", "V2")])
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
                item_id="it-1",
                job_id="job-1",
                entity_type=EntityType.VIDEO,
                source_id="v1",
                destination_id="v1",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_VIDEO,
            ),
            _make_item(
                item_id="it-2",
                job_id="job-1",
                entity_type=EntityType.VIDEO,
                source_id="v2",
                destination_id="v2",
                status=ItemStatus.ALREADY_EXISTS,
                operation=TransferOperation.NONE,
            ),
        ]
        results = verifier.verify_job(job, items)
        self.assertIn("videos", results)
        self.assertTrue(results["videos"].success)
        self.assertEqual(results["videos"].expected_count, 2)
        self.assertEqual(results["videos"].actual_count, 2)

    def test_verify_mixes(self) -> None:
        """TransferVerifier.verify_job includes 'mixes' section and verifies IDs against destination."""
        dest = FakePlatformAdapter(mixes=[record("m1", "M1")])
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
                item_id="it-1",
                job_id="job-1",
                entity_type=EntityType.MIX,
                source_id="m1",
                destination_id="m1",
                status=ItemStatus.TRANSFERRED,
                operation=TransferOperation.SAVE_MIX,
            ),
        ]
        results = verifier.verify_job(job, items)
        self.assertIn("mixes", results)
        self.assertTrue(results["mixes"].success)
        self.assertEqual(results["mixes"].expected_count, 1)
        self.assertEqual(results["mixes"].actual_count, 1)


class VideoMixRecoveryTests(unittest.TestCase):
    """RecoveryService ambiguous write reconciliation for videos and mixes."""

    def test_recovery_video_presence_semantics(self) -> None:
        """RecoveryService resolves ambiguous video write: PRESENT -> TRANSFERRED, ABSENT -> stays AMBIGUOUS, UNKNOWN -> stays AMBIGUOUS."""
        root = Path(tempfile.mkdtemp())
        item_repo = JsonTransferItemRepository(root)
        recovery = RecoveryService(item_repo)

        item_present = _make_item(
            item_id="it-1",
            job_id="job-1",
            entity_type=EntityType.VIDEO,
            source_id="v1",
            destination_id="v1",
            status=ItemStatus.AMBIGUOUS,
            mutation_state=MutationState.IN_FLIGHT,
        )
        item_repo.add_many([item_present])
        state_present = DestinationState(
            platform=Platform.TIDAL,
            video_ids=frozenset({"v1"}),
            complete_sections=frozenset({"videos"}),
        )
        resolved_present = recovery.resolve_ambiguous("job-1", state_present)
        self.assertEqual(len(resolved_present), 1)
        self.assertEqual(resolved_present[0].status, ItemStatus.TRANSFERRED)

        item_absent = _make_item(
            item_id="it-2",
            job_id="job-2",
            entity_type=EntityType.VIDEO,
            source_id="v2",
            destination_id="v2",
            status=ItemStatus.AMBIGUOUS,
            mutation_state=MutationState.IN_FLIGHT,
        )
        item_repo.add_many([item_absent])
        state_absent = DestinationState(
            platform=Platform.TIDAL,
            video_ids=frozenset(),
            complete_sections=frozenset({"videos"}),
        )
        resolved_absent = recovery.resolve_ambiguous("job-2", state_absent)
        self.assertEqual(len(resolved_absent), 0)
        persisted_absent = item_repo.load("job-2")
        self.assertEqual(len(persisted_absent), 1)
        self.assertEqual(persisted_absent[0].status, ItemStatus.AMBIGUOUS)

        item_unknown = _make_item(
            item_id="it-3",
            job_id="job-3",
            entity_type=EntityType.VIDEO,
            source_id="v3",
            destination_id="v3",
            status=ItemStatus.AMBIGUOUS,
            mutation_state=MutationState.IN_FLIGHT,
        )
        item_repo.add_many([item_unknown])
        state_unknown = DestinationState(platform=Platform.TIDAL)
        resolved_unknown = recovery.resolve_ambiguous("job-3", state_unknown)
        self.assertEqual(len(resolved_unknown), 0)
        persisted_unknown = item_repo.load("job-3")
        self.assertEqual(len(persisted_unknown), 1)
        self.assertEqual(persisted_unknown[0].status, ItemStatus.AMBIGUOUS)

    def test_recovery_mix_presence_semantics(self) -> None:
        """RecoveryService resolves ambiguous mix write: PRESENT -> TRANSFERRED, ABSENT -> stays AMBIGUOUS, UNKNOWN -> stays AMBIGUOUS."""
        root = Path(tempfile.mkdtemp())
        item_repo = JsonTransferItemRepository(root)
        recovery = RecoveryService(item_repo)

        item_present = _make_item(
            item_id="it-1",
            job_id="job-1",
            entity_type=EntityType.MIX,
            source_id="m1",
            destination_id="m1",
            status=ItemStatus.AMBIGUOUS,
            mutation_state=MutationState.IN_FLIGHT,
        )
        item_repo.add_many([item_present])
        state_present = DestinationState(
            platform=Platform.TIDAL,
            mix_ids=frozenset({"m1"}),
            complete_sections=frozenset({"mixes"}),
        )
        resolved_present = recovery.resolve_ambiguous("job-1", state_present)
        self.assertEqual(len(resolved_present), 1)
        self.assertEqual(resolved_present[0].status, ItemStatus.TRANSFERRED)

        item_absent = _make_item(
            item_id="it-2",
            job_id="job-2",
            entity_type=EntityType.MIX,
            source_id="m2",
            destination_id="m2",
            status=ItemStatus.AMBIGUOUS,
            mutation_state=MutationState.IN_FLIGHT,
        )
        item_repo.add_many([item_absent])
        state_absent = DestinationState(
            platform=Platform.TIDAL,
            mix_ids=frozenset(),
            complete_sections=frozenset({"mixes"}),
        )
        resolved_absent = recovery.resolve_ambiguous("job-2", state_absent)
        self.assertEqual(len(resolved_absent), 0)
        persisted_absent = item_repo.load("job-2")
        self.assertEqual(len(persisted_absent), 1)
        self.assertEqual(persisted_absent[0].status, ItemStatus.AMBIGUOUS)


class VideoMixEndToEndTransferTests(unittest.TestCase):
    """End-to-end TIDAL library transfer tests for videos and mixes."""

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

    def test_video_end_to_end_tidal_transfer(self) -> None:
        """Full lifecycle video transfer: creation -> analysis -> confirmation -> execution -> verification."""
        service, _ = self._setup_service()
        src_account = _make_account("src-1")
        dst_account = _make_account("dst-1")

        source = FakePlatformAdapter(videos=[record("v1", "Video 1"), record("v2", "Video 2")])
        destination = FakePlatformAdapter(videos=[record("v2", "Video 2")])  # v2 already exists

        job = service.create_job(src_account, dst_account, content=(ContentType.VIDEOS,))
        plan = service.analyze(job, source, destination)
        items = plan.items

        self.assertEqual(len(items), 2)
        v1_item = next(it for it in items if it.source_id == "v1")
        v2_item = next(it for it in items if it.source_id == "v2")

        self.assertEqual(v1_item.planned_status, ItemStatus.MATCHED)
        self.assertEqual(v1_item.operation, TransferOperation.SAVE_VIDEO)
        self.assertEqual(v2_item.planned_status, ItemStatus.ALREADY_EXISTS)

        service.confirm_plan(job, plan_id=plan.plan_id, revision=plan.revision, plan_hash=plan.plan_hash)
        result = service.execute(job, destination, confirmed=True)

        self.assertIn("v1", destination.saved_videos)
        self.assertNotIn("v2", destination.saved_videos)
        report = result["report"]
        self.assertEqual(report.transferred, 1)
        self.assertEqual(report.already_existed, 1)

        verification = result["verification"]
        self.assertIn("videos", verification)
        self.assertTrue(verification["videos"]["success"])

    def test_mix_end_to_end_tidal_transfer(self) -> None:
        """Full lifecycle mix transfer: creation -> analysis -> confirmation -> execution -> verification."""
        service, _ = self._setup_service()
        src_account = _make_account("src-1")
        dst_account = _make_account("dst-1")

        source = FakePlatformAdapter(mixes=[record("m1", "Mix 1"), record("m2", "Mix 2")])
        destination = FakePlatformAdapter(mixes=[record("m1", "Mix 1")])  # m1 already exists

        job = service.create_job(src_account, dst_account, content=(ContentType.MIXES,))
        plan = service.analyze(job, source, destination)
        items = plan.items

        self.assertEqual(len(items), 2)
        m1_item = next(it for it in items if it.source_id == "m1")
        m2_item = next(it for it in items if it.source_id == "m2")

        self.assertEqual(m1_item.planned_status, ItemStatus.ALREADY_EXISTS)
        self.assertEqual(m2_item.planned_status, ItemStatus.MATCHED)
        self.assertEqual(m2_item.operation, TransferOperation.SAVE_MIX)

        service.confirm_plan(job, plan_id=plan.plan_id, revision=plan.revision, plan_hash=plan.plan_hash)
        result = service.execute(job, destination, confirmed=True)

        self.assertIn("m2", destination.saved_mixes)
        report = result["report"]
        self.assertEqual(report.transferred, 1)
        self.assertEqual(report.already_existed, 1)

        verification = result["verification"]
        self.assertIn("mixes", verification)
        self.assertTrue(verification["mixes"]["success"])

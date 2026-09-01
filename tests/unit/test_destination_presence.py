"""Unit and regression tests for Phase 1.4C: Explicit Destination Presence Semantics.

Validates:
- DestinationPresence enum (PRESENT, ABSENT, UNKNOWN) and fail-closed defaults.
- DestinationState records complete_sections and positive proof of completeness.
- Default DestinationState represents UNKNOWN for every section, never empty-known.
- presence() and presence_in_section() authoritative queries across all entity types.
- Invalid section names fail closed before any provider operations.
- TidalAdapter and FakePlatformAdapter selective destination state reads.
- sections=None, sections=(), and explicit selection call isolation.
- Successfully-read empty sections prove ABSENT.
- Unrequested and failed sections remain UNKNOWN.
- TransferPlanner set-like planning and fail-closed handling of UNKNOWN.
- Preconditions generated exclusively from proven observations (never manufactured from UNKNOWN).
- TransferService.execute selective preflight reads and tri-state precondition validation.
- UNKNOWN preflight produces PlanValidationUnavailableError (never PlanStaleError).
- Precondition drift (PRESENT vs ABSENT) produces PlanStaleError and clears confirmation.
- RecoveryService.resolve_ambiguous tri-state consumption and ambiguous write safety.
- Playlist item sequence recovery preservation.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any

from music_transfer.app.services import TransferService
from music_transfer.core.domain import (
    Account,
    Album,
    Artist,
    LibrarySnapshot,
    Playlist,
    Track,
    TransferItem,
    TransferJob,
    TransferSettings,
)
from music_transfer.core.enums import (
    ContentType,
    DestinationPresence,
    EntityType,
    ItemStatus,
    Platform,
    PreconditionExpectation,
    TransferOperation,
)
from music_transfer.core.errors import (
    DestinationPresenceUnknownError,
    InvalidDestinationSectionError,
    PlanStaleError,
    PlanValidationUnavailableError,
    UnsupportedCapabilityError,
)
from music_transfer.core.ports import (
    DestinationState,
    ReadOnlyAdapter,
)
from music_transfer.core.ports.platform import KNOWN_DESTINATION_SECTIONS
from music_transfer.core.transfer import (
    RecoveryService,
    TransferPlanner,
    content_sections,
)
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
    JsonTransferPlanRepository,
)
from music_transfer.platforms.tidal import TidalAdapter

from tests.support import FakePlatformAdapter, album, artist, playlist, track

_LOGGER = logging.getLogger("test.destination_presence")


def new_account(identifier: str = "acc-1", platform: Platform = Platform.TIDAL) -> Account:
    return Account.create(platform, identifier, f"User {identifier}")


def make_track(title: str, identifier: str = "t1") -> Track:
    return track(title, identifier=identifier)


def make_album(title: str, identifier: str = "alb1") -> Album:
    return album(title, identifier=identifier)


def make_artist(name: str, identifier: str = "art1") -> Artist:
    return artist(name, identifier=identifier)


def make_playlist(name: str, identifier: str = "pl1", track_ids: tuple[str, ...] = ("t1",)) -> Playlist:
    return playlist(name, [make_track(f"Track {tid}", identifier=tid) for tid in track_ids], identifier=identifier)



class MockTidalClientForDestinationState:
    """Instrumented mock TidalLibraryClient to verify call scoping."""

    def __init__(
        self,
        *,
        tracks: list[Any] | None = None,
        albums: list[Any] | None = None,
        artists: list[Any] | None = None,
        playlists: list[Any] | None = None,
        fail_tracks: bool = False,
        fail_albums: bool = False,
    ) -> None:
        self._tracks = tracks if tracks is not None else []
        self._albums = albums if albums is not None else []
        self._artists = artists if artists is not None else []
        self._playlists = playlists if playlists is not None else []
        self._fail_tracks = fail_tracks
        self._fail_albums = fail_albums
        self.calls: list[str] = []

    def liked_tracks(self, progress: Any = None) -> list[Any]:
        self.calls.append("liked_tracks")
        if self._fail_tracks:
            raise RuntimeError("simulated failure in liked_tracks")
        return self._tracks

    def saved_albums(self, progress: Any = None) -> list[Any]:
        self.calls.append("saved_albums")
        if self._fail_albums:
            raise RuntimeError("simulated failure in saved_albums")
        return self._albums

    def followed_artists(self, progress: Any = None) -> list[Any]:
        self.calls.append("followed_artists")
        return self._artists

    def playlists(self, progress: Any = None) -> list[Any]:
        self.calls.append("playlists")
        return self._playlists

    def profile(self) -> Any:
        return None


# ==============================================================================
# 1. Core Model & Presence Semantics
# ==============================================================================


class DestinationPresenceModelTests(unittest.TestCase):
    """Test DestinationState model and DestinationPresence tri-state evaluation."""

    def test_default_destination_state_is_unknown_not_empty_known_state(self) -> None:
        """A freshly constructed default DestinationState represents UNKNOWN for every section."""
        state = DestinationState(platform=Platform.TIDAL)

        self.assertEqual(state.complete_sections, frozenset())
        self.assertEqual(state.incomplete_sections, ())
        self.assertEqual(state.presence(EntityType.TRACK, "123"), DestinationPresence.UNKNOWN)
        self.assertEqual(state.presence(EntityType.ALBUM, "123"), DestinationPresence.UNKNOWN)
        self.assertEqual(state.presence(EntityType.ARTIST, "123"), DestinationPresence.UNKNOWN)
        self.assertEqual(state.presence(EntityType.PLAYLIST, "123"), DestinationPresence.UNKNOWN)
        self.assertFalse(state.is_trustworthy("tracks"))
        self.assertFalse(state.is_trustworthy("albums"))

    def test_destination_presence_present(self) -> None:
        """Contained ID in a complete section returns PRESENT."""
        state = DestinationState(
            platform=Platform.TIDAL,
            track_ids=frozenset({"track-1", "track-2"}),
            complete_sections=frozenset({"tracks"}),
        )
        self.assertEqual(state.presence(EntityType.TRACK, "track-1"), DestinationPresence.PRESENT)
        self.assertEqual(state.presence_in_section("tracks", "track-1"), DestinationPresence.PRESENT)

    def test_destination_presence_absent_requires_complete_section(self) -> None:
        """Missing ID in a complete section returns ABSENT."""
        state = DestinationState(
            platform=Platform.TIDAL,
            track_ids=frozenset({"track-1"}),
            complete_sections=frozenset({"tracks"}),
        )
        self.assertEqual(state.presence(EntityType.TRACK, "track-999"), DestinationPresence.ABSENT)
        self.assertEqual(state.presence_in_section("tracks", "track-999"), DestinationPresence.ABSENT)

    def test_destination_presence_unread_section_is_unknown(self) -> None:
        """Missing ID in an unread section returns UNKNOWN."""
        state = DestinationState(
            platform=Platform.TIDAL,
            track_ids=frozenset(),
            complete_sections=frozenset(),
        )
        self.assertEqual(state.presence(EntityType.TRACK, "track-1"), DestinationPresence.UNKNOWN)
        self.assertEqual(state.presence_in_section("tracks", "track-1"), DestinationPresence.UNKNOWN)

    def test_destination_presence_failed_section_is_unknown(self) -> None:
        """Missing ID in a failed/incomplete section returns UNKNOWN."""
        state = DestinationState(
            platform=Platform.TIDAL,
            incomplete_sections=("tracks",),
        )
        self.assertEqual(state.presence(EntityType.TRACK, "track-1"), DestinationPresence.UNKNOWN)
        self.assertEqual(state.presence_in_section("tracks", "track-1"), DestinationPresence.UNKNOWN)

    def test_presence_semantics_for_tracks_albums_artists_playlists(self) -> None:
        """Matrix test verifying PRESENT, ABSENT, UNKNOWN across all 4 entity types."""
        cases = [
            (EntityType.TRACK, "tracks", "t-present", "t-absent"),
            (EntityType.ALBUM, "albums", "alb-present", "alb-absent"),
            (EntityType.ARTIST, "artists", "art-present", "art-absent"),
            (EntityType.PLAYLIST, "playlists", "pl-present", "pl-absent"),
        ]

        for entity_type, section, present_id, absent_id in cases:
            # 1. Complete section
            if section == "albums":
                state = DestinationState(platform=Platform.TIDAL, album_ids=frozenset({present_id}), complete_sections=frozenset({"albums"}))
            elif section == "artists":
                state = DestinationState(platform=Platform.TIDAL, artist_ids=frozenset({present_id}), complete_sections=frozenset({"artists"}))
            elif section == "playlists":
                state = DestinationState(platform=Platform.TIDAL, playlist_ids=frozenset({present_id}), complete_sections=frozenset({"playlists"}))
            else:
                state = DestinationState(platform=Platform.TIDAL, track_ids=frozenset({present_id}), complete_sections=frozenset({"tracks"}))

            self.assertEqual(state.presence(entity_type, present_id), DestinationPresence.PRESENT)
            self.assertEqual(state.presence(entity_type, absent_id), DestinationPresence.ABSENT)

            # 2. Unread section
            unread_state = DestinationState(platform=Platform.TIDAL)
            self.assertEqual(unread_state.presence(entity_type, present_id), DestinationPresence.UNKNOWN)
            self.assertEqual(unread_state.presence(entity_type, absent_id), DestinationPresence.UNKNOWN)

            # 3. Incomplete section
            incomplete_state = DestinationState(platform=Platform.TIDAL, incomplete_sections=(section,))
            self.assertEqual(incomplete_state.presence(entity_type, present_id), DestinationPresence.UNKNOWN)
            self.assertEqual(incomplete_state.presence(entity_type, absent_id), DestinationPresence.UNKNOWN)

    def test_unknown_section_fails_closed_with_error(self) -> None:
        """Typo or invalid section names raise InvalidDestinationSectionError."""
        state = DestinationState(platform=Platform.TIDAL, complete_sections=frozenset({"tracks"}))

        with self.assertRaises(InvalidDestinationSectionError) as ctx:
            state.presence_in_section("trakcs", "123")
        self.assertEqual(ctx.exception.section, "trakcs")

        with self.assertRaises(InvalidDestinationSectionError):
            state.presence(EntityType.VIDEO, "123")

    def test_destination_state_as_dict_metadata(self) -> None:
        """as_dict() serializes complete_sections and diagnostic counts."""
        state = DestinationState(
            platform=Platform.TIDAL,
            track_ids=frozenset({"t1", "t2"}),
            complete_sections=frozenset({"tracks"}),
            incomplete_sections=("albums",),
        )
        data = state.as_dict()
        self.assertEqual(data["platform"], "tidal")
        self.assertEqual(data["track_count"], 2)
        self.assertEqual(data["album_count"], 0)
        self.assertEqual(data["complete_sections"], ["tracks"])
        self.assertEqual(data["incomplete_sections"], ["albums"])


# ==============================================================================
# 2. Adapter Selective Destination State Reads
# ==============================================================================


class TidalAdapterDestinationStateTests(unittest.TestCase):
    """Test TidalAdapter.get_destination_state scoping and fail-closed behavior."""

    def test_destination_state_empty_selection_reads_nothing(self) -> None:
        """sections=() invokes zero provider endpoints and returns all-UNKNOWN state."""
        client = MockTidalClientForDestinationState()
        adapter = TidalAdapter(client)

        state = adapter.get_destination_state(sections=())
        self.assertEqual(client.calls, [])
        self.assertEqual(state.complete_sections, frozenset())
        self.assertEqual(state.incomplete_sections, ())
        self.assertEqual(state.presence(EntityType.TRACK, "1"), DestinationPresence.UNKNOWN)

    def test_destination_state_none_reads_all_supported_sections(self) -> None:
        """sections=None invokes all 4 canonical destination endpoints."""
        client = MockTidalClientForDestinationState(
            tracks=[make_track("T1", "t1")],
            albums=[make_album("A1", "a1")],
            artists=[make_artist("Ar1", "ar1")],
            playlists=[make_playlist("P1", "p1")],
        )
        adapter = TidalAdapter(client)

        state = adapter.get_destination_state(sections=None)
        self.assertEqual(
            client.calls,
            ["liked_tracks", "saved_albums", "followed_artists", "playlists"],
        )
        self.assertEqual(
            state.complete_sections,
            KNOWN_DESTINATION_SECTIONS,
        )
        self.assertEqual(state.presence(EntityType.TRACK, "t1"), DestinationPresence.PRESENT)
        self.assertEqual(state.presence(EntityType.TRACK, "t2"), DestinationPresence.ABSENT)
        self.assertEqual(state.presence(EntityType.ALBUM, "a1"), DestinationPresence.PRESENT)
        self.assertEqual(state.presence(EntityType.ARTIST, "ar1"), DestinationPresence.PRESENT)
        self.assertEqual(state.presence(EntityType.PLAYLIST, "p1"), DestinationPresence.PRESENT)

    def test_destination_state_explicit_selection_reads_only_requested_sections(self) -> None:
        """Explicit sections tuple reads only requested endpoints."""
        # Tracks only
        client_t = MockTidalClientForDestinationState(tracks=[make_track("T1", "t1")])
        adapter_t = TidalAdapter(client_t)
        state_t = adapter_t.get_destination_state(sections=("tracks",))
        self.assertEqual(client_t.calls, ["liked_tracks"])
        self.assertEqual(state_t.complete_sections, frozenset({"tracks"}))
        self.assertEqual(state_t.presence(EntityType.TRACK, "t1"), DestinationPresence.PRESENT)
        self.assertEqual(state_t.presence(EntityType.TRACK, "t2"), DestinationPresence.ABSENT)
        self.assertEqual(state_t.presence(EntityType.ALBUM, "a1"), DestinationPresence.UNKNOWN)

        # Albums only
        client_a = MockTidalClientForDestinationState(albums=[make_album("A1", "a1")])
        adapter_a = TidalAdapter(client_a)
        state_a = adapter_a.get_destination_state(sections=("albums",))
        self.assertEqual(client_a.calls, ["saved_albums"])
        self.assertEqual(state_a.complete_sections, frozenset({"albums"}))
        self.assertEqual(state_a.presence(EntityType.ALBUM, "a1"), DestinationPresence.PRESENT)
        self.assertEqual(state_a.presence(EntityType.TRACK, "t1"), DestinationPresence.UNKNOWN)

        # Artists only
        client_ar = MockTidalClientForDestinationState(artists=[make_artist("Ar1", "ar1")])
        adapter_ar = TidalAdapter(client_ar)
        state_ar = adapter_ar.get_destination_state(sections=("artists",))
        self.assertEqual(client_ar.calls, ["followed_artists"])
        self.assertEqual(state_ar.complete_sections, frozenset({"artists"}))

        # Playlists only
        client_p = MockTidalClientForDestinationState(playlists=[make_playlist("P1", "p1")])
        adapter_p = TidalAdapter(client_p)
        state_p = adapter_p.get_destination_state(sections=("playlists",))
        self.assertEqual(client_p.calls, ["playlists"])
        self.assertEqual(state_p.complete_sections, frozenset({"playlists"}))

    def test_destination_state_unknown_section_fails_before_provider_reads(self) -> None:
        """Unknown section name raises UnsupportedCapabilityError without calling provider."""
        client = MockTidalClientForDestinationState()
        adapter = TidalAdapter(client)

        with self.assertRaises(UnsupportedCapabilityError) as ctx:
            adapter.get_destination_state(sections=("tracks", "typo_section"))
        self.assertEqual(ctx.exception.capability, "typo_section")
        self.assertEqual(client.calls, [])

    def test_successfully_empty_destination_proves_absence(self) -> None:
        """A successfully read empty section proves ABSENT for any identifier."""
        client = MockTidalClientForDestinationState(tracks=[])
        adapter = TidalAdapter(client)

        state = adapter.get_destination_state(sections=("tracks",))
        self.assertIn("tracks", state.complete_sections)
        self.assertEqual(len(state.track_ids), 0)
        self.assertEqual(state.presence(EntityType.TRACK, "any-id"), DestinationPresence.ABSENT)

    def test_unrequested_destination_section_remains_unknown(self) -> None:
        """Unrequested sections are not marked complete or incomplete."""
        client = MockTidalClientForDestinationState(tracks=[make_track("T1", "t1")])
        adapter = TidalAdapter(client)

        state = adapter.get_destination_state(sections=("tracks",))
        self.assertIn("tracks", state.complete_sections)
        self.assertNotIn("albums", state.complete_sections)
        self.assertNotIn("albums", state.incomplete_sections)
        self.assertEqual(state.presence(EntityType.ALBUM, "a1"), DestinationPresence.UNKNOWN)

    def test_failed_destination_section_remains_unknown(self) -> None:
        """A failed read section is added to incomplete_sections and omitted from complete_sections."""
        client = MockTidalClientForDestinationState(fail_tracks=True)
        adapter = TidalAdapter(client)

        state = adapter.get_destination_state(sections=("tracks",))
        self.assertNotIn("tracks", state.complete_sections)
        self.assertIn("tracks", state.incomplete_sections)
        self.assertEqual(state.presence(EntityType.TRACK, "t1"), DestinationPresence.UNKNOWN)


# ==============================================================================
# 3. Planner Semantics & Preconditions
# ==============================================================================


class PlannerDestinationPresenceTests(unittest.TestCase):
    """Test TransferPlanner handling of PRESENT, ABSENT, UNKNOWN presence and preconditions."""

    def setUp(self) -> None:
        self.planner = TransferPlanner()

    def test_planner_present_item_becomes_already_exists(self) -> None:
        """When destination presence is PRESENT and skip_already_existing=True, status is ALREADY_EXISTS."""
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.LIKED_TRACKS,))
        source = LibrarySnapshot(
            account=new_account(),
            platform=Platform.TIDAL,
            tracks=[make_track("Track 1", "t1")],
        )
        dest_state = DestinationState(
            platform=Platform.TIDAL,
            track_ids=frozenset({"t1"}),
            complete_sections=frozenset({"tracks"}),
        )
        destination = FakePlatformAdapter()

        result = self.planner.build(job, source, ReadOnlyAdapter(destination), destination_state=dest_state)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].status, ItemStatus.ALREADY_EXISTS)
        self.assertEqual(result.plan.summary.already_exists_items, 1)

        # Precondition for PRESENT is generated
        preconditions = result.plan.preconditions
        self.assertEqual(len(preconditions), 1)
        self.assertEqual(preconditions[0].expected, PreconditionExpectation.PRESENT)
        self.assertEqual(preconditions[0].destination_id, "t1")

    def test_planner_absent_item_remains_executable(self) -> None:
        """When destination presence is proven ABSENT, status is MATCHED and an ABSENT precondition is created."""
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.LIKED_TRACKS,))
        source = LibrarySnapshot(
            account=new_account(),
            platform=Platform.TIDAL,
            tracks=[make_track("Track 1", "t1")],
        )
        dest_state = DestinationState(
            platform=Platform.TIDAL,
            track_ids=frozenset(),
            complete_sections=frozenset({"tracks"}),
        )
        destination = FakePlatformAdapter()

        result = self.planner.build(job, source, ReadOnlyAdapter(destination), destination_state=dest_state)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].status, ItemStatus.MATCHED)

        preconditions = result.plan.preconditions
        self.assertEqual(len(preconditions), 1)
        self.assertEqual(preconditions[0].expected, PreconditionExpectation.ABSENT)
        self.assertEqual(preconditions[0].destination_id, "t1")

    def test_planner_unknown_presence_does_not_become_absent(self) -> None:
        """When destination presence is UNKNOWN and skip_already_existing=True, planner fails closed."""
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.LIKED_TRACKS,))
        source = LibrarySnapshot(
            account=new_account(),
            platform=Platform.TIDAL,
            tracks=[make_track("Track 1", "t1")],
        )
        dest_state = DestinationState(
            platform=Platform.TIDAL,
            incomplete_sections=("tracks",),
        )
        destination = FakePlatformAdapter()

        with self.assertRaises(DestinationPresenceUnknownError) as ctx:
            self.planner.build(job, source, ReadOnlyAdapter(destination), destination_state=dest_state)

        self.assertEqual(ctx.exception.section, "tracks")
        self.assertEqual(ctx.exception.entity_type, EntityType.TRACK)
        self.assertEqual(ctx.exception.destination_id, "t1")
        self.assertEqual(ctx.exception.reason, "section_incomplete")

    def test_unsupported_destination_state_never_becomes_proven_absence(self) -> None:
        """Unsupported capability degraded state produces UNKNOWN and fails closed when skip_already_existing=True."""
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.LIKED_TRACKS,))
        source = LibrarySnapshot(
            account=new_account(),
            platform=Platform.TIDAL,
            tracks=[make_track("Track 1", "t1")],
        )
        destination = FakePlatformAdapter()
        destination.fail_on.add("get_destination_state")
        destination.error_factory = lambda: UnsupportedCapabilityError("supports_already_exists_detection")

        with self.assertRaises(DestinationPresenceUnknownError) as ctx:
            self.planner.build(job, source, ReadOnlyAdapter(destination))

        self.assertEqual(ctx.exception.reason, "section_not_read")

    def test_skip_already_existing_false_allows_planning_without_absent_precondition(self) -> None:
        """When skip_already_existing=False, UNKNOWN presence does not block planning but creates NO preconditions."""
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.TIDAL,
            requested_content=(ContentType.LIKED_TRACKS,),
            settings=TransferSettings(skip_already_existing=False),
        )
        source = LibrarySnapshot(
            account=new_account(),
            platform=Platform.TIDAL,
            tracks=[make_track("Track 1", "t1")],
        )
        dest_state = DestinationState(
            platform=Platform.TIDAL,
            incomplete_sections=("tracks",),
        )
        destination = FakePlatformAdapter()

        result = self.planner.build(job, source, ReadOnlyAdapter(destination), destination_state=dest_state)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].status, ItemStatus.MATCHED)
        self.assertEqual(len(result.plan.preconditions), 0)

    def test_unknown_presence_never_generates_absent_precondition(self) -> None:
        """Mandatory Invariant: UNKNOWN destination state must NEVER generate an ABSENT precondition."""
        job = TransferJob.create(
            Platform.TIDAL,
            Platform.TIDAL,
            requested_content=(ContentType.LIKED_TRACKS,),
            settings=TransferSettings(skip_already_existing=False),
        )
        source = LibrarySnapshot(
            account=new_account(),
            platform=Platform.TIDAL,
            tracks=[make_track("Track 1", "t1")],
        )
        unread_state = DestinationState(platform=Platform.TIDAL)
        destination = FakePlatformAdapter()

        result = self.planner.build(job, source, ReadOnlyAdapter(destination), destination_state=unread_state)
        self.assertEqual(len(result.plan.preconditions), 0)

    def test_known_absence_generates_absent_precondition(self) -> None:
        """Proven ABSENT presence generates an ABSENT precondition for planned writes."""
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.LIKED_TRACKS,))
        source = LibrarySnapshot(
            account=new_account(),
            platform=Platform.TIDAL,
            tracks=[make_track("Track 1", "t1")],
        )
        complete_empty_state = DestinationState(
            platform=Platform.TIDAL,
            complete_sections=frozenset({"tracks"}),
        )
        destination = FakePlatformAdapter()

        result = self.planner.build(job, source, ReadOnlyAdapter(destination), destination_state=complete_empty_state)
        absent_preconditions = [p for p in result.plan.preconditions if p.expected == PreconditionExpectation.ABSENT]
        self.assertEqual(len(absent_preconditions), 1)
        self.assertEqual(absent_preconditions[0].destination_id, "t1")

    def test_known_presence_generates_present_precondition_when_applicable(self) -> None:
        """Proven PRESENT presence generates a PRESENT precondition for ALREADY_EXISTS items."""
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.LIKED_TRACKS,))
        source = LibrarySnapshot(
            account=new_account(),
            platform=Platform.TIDAL,
            tracks=[make_track("Track 1", "t1")],
        )
        present_state = DestinationState(
            platform=Platform.TIDAL,
            track_ids=frozenset({"t1"}),
            complete_sections=frozenset({"tracks"}),
        )
        destination = FakePlatformAdapter()

        result = self.planner.build(job, source, ReadOnlyAdapter(destination), destination_state=present_state)
        present_preconditions = [p for p in result.plan.preconditions if p.expected == PreconditionExpectation.PRESENT]
        self.assertEqual(len(present_preconditions), 1)
        self.assertEqual(present_preconditions[0].destination_id, "t1")

    def test_planner_selective_destination_state_reads(self) -> None:
        """Planner requests only relevant destination sections based on content types."""
        self.assertEqual(content_sections((ContentType.LIKED_TRACKS,)), ("tracks",))
        self.assertEqual(content_sections((ContentType.SAVED_ALBUMS,)), ("albums",))
        self.assertEqual(content_sections((ContentType.FOLLOWED_ARTISTS,)), ("artists",))
        self.assertEqual(content_sections((ContentType.PLAYLISTS,)), ("playlists",))
        self.assertEqual(
            content_sections((ContentType.LIKED_TRACKS, ContentType.SAVED_ALBUMS)),
            ("albums", "tracks"),
        )


# ==============================================================================
# 4. Execution Preflight & Preconditions
# ==============================================================================


class ExecutionPreflightTests(unittest.TestCase):
    """Test TransferService.execute precondition validation and preflight scoping."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.jobs = JsonTransferJobRepository(root / "jobs")
        self.items = JsonTransferItemRepository(root / "items")
        self.plans = JsonTransferPlanRepository(root / "plans")
        self.service = TransferService(
            self.jobs,
            self.items,
            plans_repository=self.plans,
            logger=_LOGGER,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_execution_preflight_reads_only_precondition_sections(self) -> None:
        """Preflight queries destination state only for sections appearing in preconditions."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.LIKED_TRACKS,))
        source = FakePlatformAdapter(tracks=[make_track("Track 1", "t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(job, plan_id=plan.plan_id, revision=plan.revision, plan_hash=plan.plan_hash)

        # Track destination state calls
        destination_calls: list[Any] = []
        orig_get_state = destination.get_destination_state

        def instrumented_get_state(sections=None) -> DestinationState:
            destination_calls.append(sections)
            return orig_get_state(sections)

        destination.get_destination_state = instrumented_get_state  # type: ignore[method-assign]

        self.service.execute(job, destination, confirmed=True)
        self.assertIn(("tracks",), destination_calls)
        # Should NOT have requested all sections
        self.assertNotIn(None, destination_calls)

    def test_execution_precondition_unknown_is_validation_unavailable(self) -> None:
        """When destination state is UNKNOWN during preflight, PlanValidationUnavailableError is raised."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.LIKED_TRACKS,))
        source = FakePlatformAdapter(tracks=[make_track("Track 1", "t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(job, plan_id=plan.plan_id, revision=plan.revision, plan_hash=plan.plan_hash)

        # Preflight returns unread / incomplete state
        destination.get_destination_state = lambda sections=None: DestinationState(  # type: ignore[method-assign]
            platform=Platform.TIDAL,
            incomplete_sections=("tracks",),
        )

        with self.assertRaises(PlanValidationUnavailableError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        # Job confirmation remains intact (not marked stale)
        self.assertEqual(job.confirmed_plan_id, plan.plan_id)

    def test_execution_expected_absent_actual_present_is_stale(self) -> None:
        """When preflight observes PRESENT for an expected ABSENT item, PlanStaleError is raised and confirmation cleared."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.LIKED_TRACKS,))
        source = FakePlatformAdapter(tracks=[make_track("Track 1", "t1")])
        destination = FakePlatformAdapter()

        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(job, plan_id=plan.plan_id, revision=plan.revision, plan_hash=plan.plan_hash)

        # Destination drifted: item was added concurrently
        destination.saved_tracks.append("t1")

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertIsNone(job.confirmed_plan_id)
        self.assertEqual(job.error_code, "plan_stale")

    def test_execution_expected_present_actual_absent_is_stale(self) -> None:
        """When preflight observes ABSENT for an expected PRESENT item, PlanStaleError is raised and confirmation cleared."""
        job = self.service.create_job(Platform.TIDAL, Platform.TIDAL, content=(ContentType.LIKED_TRACKS,))
        source = FakePlatformAdapter(tracks=[make_track("Track 1", "t1")])
        destination = FakePlatformAdapter(tracks=[make_track("Track 1", "t1")])

        # Analyze when item already exists
        plan = self.service.analyze(job, source, destination)
        self.service.confirm_plan(job, plan_id=plan.plan_id, revision=plan.revision, plan_hash=plan.plan_hash)

        # Destination drifted: existing item was deleted
        destination.tracks.clear()

        with self.assertRaises(PlanStaleError):
            self.service.execute(job, destination, confirmed=True)

        self.assertEqual(destination.write_calls, [])
        self.assertIsNone(job.confirmed_plan_id)
        self.assertEqual(job.error_code, "plan_stale")


# ==============================================================================
# 5. Recovery Semantics & Invariants
# ==============================================================================


class RecoveryDestinationPresenceTests(unittest.TestCase):
    """Test RecoveryService handling of tri-state presence and ambiguous mutation safety."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.items = JsonTransferItemRepository(root / "items")
        self.recovery = RecoveryService(self.items, _LOGGER)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_recovery_present_resolves_ambiguous_as_transferred(self) -> None:
        """When ambiguous item is observed PRESENT, it is marked TRANSFERRED and never replayed."""
        job_id = "job-rec-1"
        item = TransferItem.create(
            job_id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-1",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.status = ItemStatus.AMBIGUOUS
        item.destination_id = "dst-t1"
        self.items.add_many([item])

        state = DestinationState(
            platform=Platform.TIDAL,
            track_ids=frozenset({"dst-t1"}),
            complete_sections=frozenset({"tracks"}),
        )

        resolved = self.recovery.resolve_ambiguous(job_id, state)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].status, ItemStatus.TRANSFERRED)

        # Re-check item in repository
        stored = self.items.list_for_job(job_id)
        self.assertEqual(stored[0].status, ItemStatus.TRANSFERRED)

    def test_recovery_unknown_keeps_ambiguous_unresolved(self) -> None:
        """When destination state is UNKNOWN, ambiguous item remains AMBIGUOUS."""
        job_id = "job-rec-2"
        item = TransferItem.create(
            job_id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-1",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.status = ItemStatus.AMBIGUOUS
        item.destination_id = "dst-t1"
        self.items.add_many([item])

        state = DestinationState(
            platform=Platform.TIDAL,
            incomplete_sections=("tracks",),
        )

        resolved = self.recovery.resolve_ambiguous(job_id, state)
        self.assertEqual(len(resolved), 0)

        stored = self.items.list_for_job(job_id)
        self.assertEqual(stored[0].status, ItemStatus.AMBIGUOUS)

    def test_recovery_absent_is_distinct_from_unknown(self) -> None:
        """When destination state is proven ABSENT, ambiguous item remains AMBIGUOUS awaiting explicit retry policy."""
        job_id = "job-rec-3"
        item = TransferItem.create(
            job_id,
            EntityType.TRACK,
            Platform.TIDAL,
            "track-1",
            Platform.TIDAL,
            operation=TransferOperation.SAVE_TRACK,
        )
        item.status = ItemStatus.AMBIGUOUS
        item.destination_id = "dst-t1"
        self.items.add_many([item])

        state = DestinationState(
            platform=Platform.TIDAL,
            track_ids=frozenset({"other-track"}),
            complete_sections=frozenset({"tracks"}),
        )

        resolved = self.recovery.resolve_ambiguous(job_id, state)
        self.assertEqual(len(resolved), 0)

        stored = self.items.list_for_job(job_id)
        self.assertEqual(stored[0].status, ItemStatus.AMBIGUOUS)

    def test_playlist_sequence_recovery_still_uses_ordered_playlist_item_ids(self) -> None:
        """Playlist item sequence recovery is preserved and independent of set-based DestinationPresence."""
        destination = FakePlatformAdapter()
        created_id = destination.create_playlist(Playlist(Platform.TIDAL, "pl1", "Playlist 1"))
        destination.add_playlist_item(created_id, "t1")
        destination.add_playlist_item(created_id, "t2")

        actual_ids = destination.playlist_item_ids(created_id)
        self.assertEqual(actual_ids, ["t1", "t2"])



if __name__ == "__main__":
    unittest.main()

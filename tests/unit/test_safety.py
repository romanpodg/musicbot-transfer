"""Safety invariants: no writes while planning, no writes without confirmation.

Three invariants from the specification are enforced here structurally rather
than by convention:

* **Invariant B** - the plan performs no destination mutation.  The planner
  wraps the destination in :class:`ReadOnlyAdapter`, so a bug in the planner
  fails loudly at runtime instead of silently writing.
* **Confirmation** - ``TransferService.execute`` refuses to write unless the
  caller passes ``confirmed=True``.  The gate is in the application layer, so a
  forgotten UI check cannot mutate a library.
* **No fake capabilities** - an unsupported operation raises
  ``UnsupportedCapabilityError``; it never returns ``True`` or an empty result
  that looks like success.
"""

from __future__ import annotations

import logging
import unittest

from music_transfer.core.domain import (
    AccountProfile,
    Album,
    Artist,
    LibrarySnapshot,
    Track,
    TransferJob,
    TransferSettings,
)
from music_transfer.core.enums import ContentType, EntityType, OperationKind, Platform
from music_transfer.core.errors import (
    ConfirmationRequired,
    UnsupportedCapabilityError,
    UnsupportedTransferContentError,
)
from music_transfer.core.matching import TrackMatcher
from music_transfer.core.ports import (
    DestinationState,
    MusicPlatformAdapter,
    MusicPlatformReadPort,
    PlatformCapabilities,
    ReadOnlyAdapter,
    operation_kind,
)
from music_transfer.core.transfer import (
    TransferPlanner,
    require_transfer_content_spec,
    validate_transfer_content_support,
)
from music_transfer.platforms.registry import default_registry

from tests.support import FakePlatformAdapter, track


def new_account(identifier: str):
    """Return a TIDAL account with the given platform account id."""

    from music_transfer.core.domain import Account

    return Account.create(Platform.TIDAL, identifier, identifier)


def source_snapshot() -> LibrarySnapshot:
    """A small, complete source snapshot used by the safety tests."""

    return LibrarySnapshot(
        account=AccountProfile("1", "source", Platform.TIDAL),
        platform=Platform.TIDAL,
        tracks=[track("Song", identifier="1")],
    )


class MinimalReadPort:
    """A minimal implementation of MusicPlatformReadPort with zero mutation methods."""

    def __init__(self, platform: Platform = Platform.TIDAL, tracks: list[Track] | None = None) -> None:
        self._platform = platform
        self._tracks = list(tracks or [])
        self.capabilities = PlatformCapabilities(
            platform=platform,
            read_liked_tracks=True,
            write_liked_tracks=True,
            search_tracks=True,
            supports_already_exists_detection=True,
        )

    @property
    def platform(self) -> Platform:
        return self._platform

    def get_destination_state(self, sections: tuple[str, ...] | None = None) -> DestinationState:
        return DestinationState(
            platform=self._platform,
            track_ids=frozenset(t.source_id for t in self._tracks),
        )

    def search_track(self, track: Track, limit: int = 5) -> list[Track]:
        return [t for t in self._tracks if t.title == track.title][:limit]

    def search_album(self, album: Album, limit: int = 5) -> list[Album]:
        return []

    def search_artist(self, artist: Artist, limit: int = 5) -> list[Artist]:
        return []

    def can_reuse_identifier(self, entity_type: EntityType, source: Platform) -> bool:
        return source == self._platform


class PlanningMakesNoWrites(unittest.TestCase):
    """Invariant B: nothing is written while a plan is being built."""

    def test_planner_performs_no_writes(self) -> None:
        """Planning leaves the destination completely untouched."""

        destination = FakePlatformAdapter(display_name="destination")
        job = TransferJob.create(
            Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.LIKED_TRACKS,)
        )
        TransferPlanner(TrackMatcher()).build(job, source_snapshot(), ReadOnlyAdapter(destination))

        self.assertEqual(
            destination.write_calls,
            [],
            "planning must not call a single mutating method",
        )
        self.assertEqual(destination.saved_tracks, [])

    def test_planner_accepts_minimal_read_port(self) -> None:
        """Planner builds successfully when given a minimal read-only port."""

        minimal = MinimalReadPort()
        self.assertIsInstance(minimal, MusicPlatformReadPort)
        job = TransferJob.create(
            Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.LIKED_TRACKS,)
        )
        result = TransferPlanner(TrackMatcher()).build(job, source_snapshot(), minimal)
        self.assertEqual(result.summary.total_items, 1)
        self.assertEqual(result.summary.matched_items, 1)

    def test_planner_does_not_require_full_adapter(self) -> None:
        """Planner does not require full MusicPlatformAdapter inheritance or write methods."""

        minimal = MinimalReadPort()
        self.assertFalse(isinstance(minimal, MusicPlatformAdapter))
        self.assertFalse(hasattr(minimal, "save_track"))
        self.assertFalse(hasattr(minimal, "create_playlist"))

        job = TransferJob.create(
            Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.LIKED_TRACKS,)
        )
        result = TransferPlanner(TrackMatcher()).build(job, source_snapshot(), minimal)
        self.assertIsNotNone(result.plan)

    def test_planning_read_facade_exposes_no_known_write_methods(self) -> None:
        """The read-only facade has no mutating or destructive methods."""

        destination = ReadOnlyAdapter(FakePlatformAdapter())
        for method in MusicPlatformAdapter.MUTATING_METHODS | MusicPlatformAdapter.DESTRUCTIVE_METHODS:
            with self.subTest(method=method):
                self.assertFalse(hasattr(destination, method))
                with self.assertRaises(AttributeError):
                    getattr(destination, method)("arg1")

    def test_planning_read_facade_rejects_unknown_adapter_method(self) -> None:
        """An unknown future adapter method is unreachable through the read-only facade."""

        class AdapterWithFutureMethod(FakePlatformAdapter):
            def future_remote_mutation(self, arg: str) -> None:
                self.write_calls.append(("future_remote_mutation", (arg,)))

        fake = AdapterWithFutureMethod()
        read_only = ReadOnlyAdapter(fake)

        self.assertTrue(hasattr(fake, "future_remote_mutation"))
        self.assertFalse(hasattr(read_only, "future_remote_mutation"))
        with self.assertRaises(AttributeError):
            read_only.future_remote_mutation("payload")  # type: ignore[attr-defined]
        self.assertEqual(fake.write_calls, [])

    def test_planning_read_facade_has_no_public_full_adapter_escape(self) -> None:
        """The read facade does not expose a public .inner property to the full adapter."""

        destination = ReadOnlyAdapter(FakePlatformAdapter())
        self.assertFalse(hasattr(destination, "inner"))
        with self.assertRaises(AttributeError):
            _ = destination.inner

    def test_planning_read_facade_forwards_reads(self) -> None:
        """Planning read methods pass through to the underlying adapter."""

        fake = FakePlatformAdapter(tracks=[track("A", identifier="a")])
        destination = ReadOnlyAdapter(fake)

        self.assertIs(destination.platform, Platform.TIDAL)
        self.assertTrue(destination.capabilities.supports("read_liked_tracks"))
        self.assertTrue(destination.can_reuse_identifier(EntityType.TRACK, Platform.TIDAL))
        self.assertEqual(len(destination.search_track(track("A"))), 1)
        state = destination.get_destination_state()
        self.assertTrue(state.has_track("a"))

    def test_operation_kind_classifies_methods(self) -> None:
        """Every adapter method is classified read / mutating / destructive."""

        self.assertIs(operation_kind(FakePlatformAdapter, "save_track"), OperationKind.MUTATING)
        self.assertIs(operation_kind(FakePlatformAdapter, "delete_playlist"), OperationKind.DESTRUCTIVE)
        self.assertIs(operation_kind(FakePlatformAdapter, "get_liked_tracks"), OperationKind.READ)

    def test_operation_kind_rejects_unknown_methods(self) -> None:
        """An unknown method is never implicitly classified as safe READ."""

        with self.assertRaises(UnsupportedCapabilityError) as ctx:
            operation_kind(FakePlatformAdapter, "future_remote_mutation")
        self.assertEqual(ctx.exception.code, "unknown_operation")
        self.assertEqual(ctx.exception.capability, "future_remote_mutation")

    def test_mutating_and_destructive_sets_do_not_overlap(self) -> None:
        """Method classification sets are mutually disjoint."""

        self.assertFalse(
            MusicPlatformAdapter.MUTATING_METHODS
            & MusicPlatformAdapter.DESTRUCTIVE_METHODS
        )
        self.assertFalse(
            MusicPlatformAdapter.READ_METHODS
            & MusicPlatformAdapter.MUTATING_METHODS
        )
        self.assertFalse(
            MusicPlatformAdapter.READ_METHODS
            & MusicPlatformAdapter.DESTRUCTIVE_METHODS
        )


class ContentSupportValidation(unittest.TestCase):
    """Authoritative transfer content support: source ∩ destination ∩ engine."""

    def _service(self):
        import tempfile
        from pathlib import Path

        from music_transfer.app.services import TransferService
        from music_transfer.infrastructure.persistence import (
            JsonTransferItemRepository,
            JsonTransferJobRepository,
        )

        root = Path(tempfile.mkdtemp())
        return TransferService(
            JsonTransferJobRepository(root), JsonTransferItemRepository(root)
        )

    def test_supported_track_transfer_passes_content_validation(self) -> None:
        """A supported content type with matching capabilities passes validation."""

        source_cap = PlatformCapabilities(platform=Platform.TIDAL, read_liked_tracks=True)
        dest_cap = PlatformCapabilities(platform=Platform.TIDAL, write_liked_tracks=True)
        validate_transfer_content_support((ContentType.LIKED_TRACKS,), source_cap, dest_cap)

    def test_source_read_capability_missing_is_semantic_error(self) -> None:
        """Missing source read capability raises UnsupportedTransferContentError."""

        source_cap = PlatformCapabilities(platform=Platform.TIDAL, read_liked_tracks=False)
        dest_cap = PlatformCapabilities(platform=Platform.TIDAL, write_liked_tracks=True)
        with self.assertRaises(UnsupportedTransferContentError) as ctx:
            validate_transfer_content_support((ContentType.LIKED_TRACKS,), source_cap, dest_cap)
        self.assertEqual(ctx.exception.code, "unsupported_transfer_content")
        self.assertEqual(ctx.exception.reason, "source_read_unsupported")
        self.assertEqual(ctx.exception.content_type, ContentType.LIKED_TRACKS)
        self.assertEqual(ctx.exception.capability, "read_liked_tracks")

    def test_destination_write_capability_missing_is_semantic_error(self) -> None:
        """Missing destination write capability raises UnsupportedTransferContentError."""

        source_cap = PlatformCapabilities(platform=Platform.TIDAL, read_liked_tracks=True)
        dest_cap = PlatformCapabilities(platform=Platform.TIDAL, write_liked_tracks=False)
        with self.assertRaises(UnsupportedTransferContentError) as ctx:
            validate_transfer_content_support((ContentType.LIKED_TRACKS,), source_cap, dest_cap)
        self.assertEqual(ctx.exception.code, "unsupported_transfer_content")
        self.assertEqual(ctx.exception.reason, "destination_write_unsupported")
        self.assertEqual(ctx.exception.content_type, ContentType.LIKED_TRACKS)
        self.assertEqual(ctx.exception.capability, "write_liked_tracks")

    def test_declared_but_engine_unsupported_content_is_semantic_error(self) -> None:
        """Declared content without engine implementation is rejected regardless of capability flags."""

        source_cap = PlatformCapabilities(
            platform=Platform.TIDAL, read_videos=True, read_mixes=True
        )
        dest_cap = PlatformCapabilities(
            platform=Platform.TIDAL, write_videos=True, write_mixes=True
        )
        for content_type in (ContentType.VIDEOS, ContentType.MIXES):
            with self.subTest(content_type=content_type):
                with self.assertRaises(UnsupportedTransferContentError) as ctx:
                    validate_transfer_content_support((content_type,), source_cap, dest_cap)
                self.assertEqual(ctx.exception.code, "unsupported_transfer_content")
                self.assertEqual(ctx.exception.reason, "engine_not_implemented")
                self.assertEqual(ctx.exception.content_type, content_type)

    def test_declared_but_engine_unsupported_content_never_raises_raw_key_error(self) -> None:
        """Resolving unimplemented content raises UnsupportedTransferContentError, never raw KeyError."""

        for content_type in (ContentType.VIDEOS, ContentType.MIXES):
            with self.subTest(content_type=content_type):
                with self.assertRaises(UnsupportedTransferContentError) as ctx:
                    require_transfer_content_spec(content_type)
                self.assertEqual(ctx.exception.reason, "engine_not_implemented")

    def test_mixed_request_with_unsupported_content_is_rejected_as_a_whole(self) -> None:
        """A mixed request is fail-closed and rejected as a whole without partial filtering."""

        source_cap = FakePlatformAdapter.CAPABILITIES
        dest_cap = FakePlatformAdapter.CAPABILITIES
        with self.assertRaises(UnsupportedTransferContentError) as ctx:
            validate_transfer_content_support(
                (ContentType.LIKED_TRACKS, ContentType.PLAYLISTS, ContentType.VIDEOS),
                source_cap,
                dest_cap,
            )
        self.assertEqual(ctx.exception.content_type, ContentType.VIDEOS)
        self.assertEqual(ctx.exception.reason, "engine_not_implemented")

    def test_playlist_requires_create_playlist_capability(self) -> None:
        """Playlist transfer requires destination create_playlists capability."""

        source_cap = PlatformCapabilities(platform=Platform.TIDAL, read_playlists=True)
        dest_cap = PlatformCapabilities(
            platform=Platform.TIDAL, create_playlists=False, write_playlist_items=True
        )
        with self.assertRaises(UnsupportedTransferContentError) as ctx:
            validate_transfer_content_support((ContentType.PLAYLISTS,), source_cap, dest_cap)
        self.assertEqual(ctx.exception.reason, "destination_write_unsupported")
        self.assertEqual(ctx.exception.capability, "create_playlists")

    def test_playlist_requires_playlist_item_write_capability(self) -> None:
        """Playlist transfer requires destination write_playlist_items capability."""

        source_cap = PlatformCapabilities(platform=Platform.TIDAL, read_playlists=True)
        dest_cap = PlatformCapabilities(
            platform=Platform.TIDAL, create_playlists=True, write_playlist_items=False
        )
        with self.assertRaises(UnsupportedTransferContentError) as ctx:
            validate_transfer_content_support((ContentType.PLAYLISTS,), source_cap, dest_cap)
        self.assertEqual(ctx.exception.reason, "destination_write_unsupported")
        self.assertEqual(ctx.exception.capability, "write_playlist_items")

    def test_unsupported_request_does_not_export_source(self) -> None:
        """An unsupported request fails before calling source export_library."""

        class TrackingSourceAdapter(FakePlatformAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.export_calls = 0

            def export_library(self, sections=None, progress=None) -> LibrarySnapshot:
                self.export_calls += 1
                return super().export_library(sections=sections, progress=progress)

        service = self._service()
        source = TrackingSourceAdapter()
        destination = FakePlatformAdapter()
        job = service.create_job(
            new_account("src-1"),
            new_account("dst-1"),
            content=(ContentType.VIDEOS,),
        )

        with self.assertRaises(UnsupportedTransferContentError):
            service.analyze(job, source, destination)

        self.assertEqual(source.export_calls, 0)
        self.assertEqual(destination.write_calls, [])

    def test_unsupported_request_produces_zero_destination_writes(self) -> None:
        """An unsupported request never performs any destination writes."""

        service = self._service()
        source = FakePlatformAdapter(tracks=[track("A", identifier="a")])
        destination = FakePlatformAdapter()
        job = service.create_job(
            new_account("src-1"),
            new_account("dst-1"),
            content=(ContentType.VIDEOS,),
        )

        with self.assertRaises(UnsupportedTransferContentError):
            service.analyze(job, source, destination)

        self.assertEqual(destination.write_calls, [])


class UnsupportedCapabilityBehaviour(unittest.TestCase):
    """Unsupported operations must be loud, never silently successful."""

    def test_default_adapter_write_raises_instead_of_returning_true(self) -> None:
        """A bare adapter raises rather than pretending the write worked."""

        class Bare(MusicPlatformAdapter):
            """An adapter implementing only the abstract minimum."""

            @property
            def platform(self) -> Platform:
                return Platform.SPOTIFY

            @property
            def capabilities(self) -> PlatformCapabilities:
                return PlatformCapabilities(platform=Platform.SPOTIFY)

            def get_profile(self):
                return AccountProfile("1", "bare", Platform.SPOTIFY)

        with self.assertRaises(UnsupportedCapabilityError):
            Bare().save_track("1")
        with self.assertRaises(UnsupportedCapabilityError):
            Bare().create_folder("name", "root")

    def test_registry_rejects_unregistered_platform(self) -> None:
        """Asking for a platform with no adapter raises."""

        registry = default_registry()
        with self.assertRaises(UnsupportedCapabilityError) as context:
            registry.create(Platform.SPOTIFY, None)
        self.assertEqual(context.exception.code, "platform_not_registered")

    def test_registered_platform_creates_an_adapter(self) -> None:
        """TIDAL is registered and creatable."""

        registry = default_registry()
        self.assertIn(Platform.TIDAL, registry.registered())
        adapter = registry.create(Platform.TIDAL, None)
        self.assertIs(adapter.platform, Platform.TIDAL)

    def test_planned_platforms_are_absent_not_faked(self) -> None:
        """Spotify, Apple, Deezer, and YouTube have no fake adapters."""

        registry = default_registry()
        for platform in (
            Platform.SPOTIFY,
            Platform.APPLE_MUSIC,
            Platform.DEEZER,
            Platform.YOUTUBE_MUSIC,
        ):
            with self.subTest(platform=platform.value):
                self.assertNotIn(platform, registry.registered())

    def test_capabilities_require_raises_when_missing(self) -> None:
        """``capabilities.require`` is the engine's guard, not a name check."""

        capabilities = PlatformCapabilities(
            platform=Platform.SPOTIFY, read_liked_tracks=False
        )
        with self.assertRaises(UnsupportedCapabilityError):
            capabilities.require("read_liked_tracks")
        self.assertFalse(capabilities.supports("read_liked_tracks"))


class ConfirmationGate(unittest.TestCase):
    """Execution requires an explicit confirmation decision."""

    def _service(self):
        """Build a transfer service backed by in-memory repositories."""

        import tempfile
        from pathlib import Path

        from music_transfer.app.services import TransferService
        from music_transfer.infrastructure.persistence import (
            JsonTransferItemRepository,
            JsonTransferJobRepository,
        )

        root = Path(tempfile.mkdtemp())
        return TransferService(
            JsonTransferJobRepository(root), JsonTransferItemRepository(root)
        )

    def test_execute_without_confirmation_raises(self) -> None:
        """An unconfirmed execution is refused before any write."""

        service = self._service()
        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL)
        with self.assertRaises(ConfirmationRequired) as context:
            service.execute(job, FakePlatformAdapter(), confirmed=False)
        self.assertEqual(context.exception.code, "transfer_confirmation_required")

    def test_same_account_transfer_is_rejected(self) -> None:
        """A transfer onto itself refuses to start at all."""

        from music_transfer.core.domain import Account
        from music_transfer.core.errors import TransferConfigurationError

        service = self._service()
        account = Account.create(Platform.TIDAL, "1", "Roman")
        with self.assertRaises(TransferConfigurationError) as context:
            service.create_job(account, account)
        self.assertEqual(context.exception.code, "same_account_transfer")

    def test_different_tidal_accounts_are_allowed(self) -> None:
        """TIDAL account A -> TIDAL account B is a legitimate transfer."""

        from music_transfer.core.domain import Account

        service = self._service()
        source = Account.create(Platform.TIDAL, "1", "A")
        destination = Account.create(Platform.TIDAL, "2", "B")
        job = service.create_job(source, destination)
        self.assertEqual(job.source_account_id, source.id)
        self.assertEqual(job.destination_account_id, destination.id)

    def test_dry_run_makes_no_writes(self) -> None:
        """A dry run plans and simulates without touching the destination."""

        service = self._service()
        destination = FakePlatformAdapter()
        job = service.create_job(
            new_account("src-1"),
            new_account("dst-1"),
            settings=TransferSettings(dry_run=True),
        )
        service.analyze(job, FakePlatformAdapter(tracks=[track("A", identifier="a")]), destination)
        service.confirm_plan(
            job,
            plan_id=job.active_plan_id,
            revision=job.active_plan_revision,
            plan_hash=job.active_plan_hash,
        )
        service.execute(job, destination, confirmed=True)
        self.assertEqual(destination.write_calls, [])


class LoggingSafety(unittest.TestCase):
    """Logs must never leak credentials or tokens."""

    def test_secret_filter_redacts_token_shaped_values(self) -> None:
        """A token-shaped value is scrubbed from the rendered message."""

        from music_transfer.infrastructure.logging import SecretRedactionFilter

        record = logging.LogRecord(
            "test", logging.INFO, "path", 1,
            "session token_type=Bearer access_token=abc123secret", None, None,
        )
        SecretRedactionFilter().filter(record)
        self.assertNotIn("abc123secret", record.getMessage())

    def test_secret_filter_leaves_ordinary_text(self) -> None:
        """Redaction is targeted; it does not mangle normal log lines."""

        from music_transfer.infrastructure.logging import SecretRedactionFilter

        record = logging.LogRecord(
            "test", logging.INFO, "path", 1, "event=job_created job_id=abc", None, None
        )
        SecretRedactionFilter().filter(record)
        self.assertEqual(record.getMessage(), "event=job_created job_id=abc")


if __name__ == "__main__":
    unittest.main()

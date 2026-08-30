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

from music_transfer.core.domain import TransferSettings
from music_transfer.core.enums import ContentType, EntityType, ItemStatus, Platform
from music_transfer.core.errors import (
    ConfirmationRequired,
    UnsupportedCapabilityError,
)
from music_transfer.core.enums import OperationKind
from music_transfer.core.ports import (
    MusicPlatformAdapter,
    PlatformCapabilities,
    ReadOnlyAdapter,
    operation_kind,
)
from music_transfer.core.transfer import TransferPlanner
from music_transfer.platforms.registry import PlatformRegistry, default_registry
from tests.support import FakePlatformAdapter, track
from tests.support import artist
from music_transfer.core.domain import LibrarySnapshot, AccountProfile
from music_transfer.core.domain import TransferJob
from music_transfer.core.matching import TrackMatcher


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


class PlanningMakesNoWrites(unittest.TestCase):
    """Invariant B: nothing is written while a plan is being built."""

    def test_planner_performs_no_writes(self) -> None:
        """Planning leaves the destination completely untouched."""

        destination = FakePlatformAdapter(display_name="destination")
        job = TransferJob.create(
            Platform.TIDAL, Platform.TIDAL, requested_content=(ContentType.LIKED_TRACKS,)
        )
        TransferPlanner(TrackMatcher()).build(job, source_snapshot(), destination)

        self.assertEqual(
            destination.write_calls,
            [],
            "planning must not call a single mutating method",
        )
        self.assertEqual(destination.saved_tracks, [])

    def test_read_only_adapter_blocks_writes(self) -> None:
        """The read-only wrapper turns any write into an immediate error."""

        destination = ReadOnlyAdapter(FakePlatformAdapter())
        with self.assertRaises(UnsupportedCapabilityError):
            destination.save_track("1")
        with self.assertRaises(UnsupportedCapabilityError):
            destination.follow_artist("1")
        with self.assertRaises(UnsupportedCapabilityError):
            destination.add_playlist_item("p", "t")

    def test_read_only_adapter_still_allows_reads(self) -> None:
        """Reads pass through, so planning and verification still work."""

        destination = ReadOnlyAdapter(FakePlatformAdapter(tracks=[track("A", identifier="a")]))
        self.assertEqual([item.source_id for item in destination.get_liked_tracks()], ["a"])
        self.assertEqual(destination.get_profile().display_name, "fake")

    def test_operation_kind_classifies_methods(self) -> None:
        """Every adapter method is classified read / mutating / destructive."""

        self.assertIs(operation_kind(FakePlatformAdapter, "save_track"), OperationKind.MUTATING)
        self.assertIs(operation_kind(FakePlatformAdapter, "delete_playlist"), OperationKind.DESTRUCTIVE)
        self.assertIs(operation_kind(FakePlatformAdapter, "get_liked_tracks"), OperationKind.READ)

    def test_mutating_and_destructive_sets_do_not_overlap(self) -> None:
        """A method is never both a write and a delete."""

        self.assertFalse(
            MusicPlatformAdapter.MUTATING_METHODS
            & MusicPlatformAdapter.DESTRUCTIVE_METHODS
        )


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

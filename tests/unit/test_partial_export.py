"""Unit and regression tests for Phase 1.4B: Real Source-Side Partial Export.

Guarantees tested:
- TransferService passes derived snapshot sections to source.export_library.
- TidalLibraryClient and TidalAdapter selectively fetch only requested sections.
- Full export remains default when sections is None.
- sections=() fetches zero content sections and returns a clean snapshot.
- Unknown section names fail closed before any provider read.
- Unrequested sections are not marked incomplete.
- Failed requested sections are marked incomplete.
- Progress callbacks emit only for requested sections.
- Provider API call reduction is verified for track-only, album-only, artist-only,
  playlist-only, and mixed selections.
- Selective playlist export preserves item order, duplicates, and matches full export.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from music_transfer.app.services import TransferService
from music_transfer.app.services.transfer_service import content_sections
from music_transfer.core.domain import (
    Account,
    LibrarySnapshot,
)
from music_transfer.core.enums import ContentType, Platform
from music_transfer.core.errors import UnsupportedCapabilityError
from music_transfer.core.ports import PlatformCapabilities
from music_transfer.infrastructure.persistence import (
    JsonTransferItemRepository,
    JsonTransferJobRepository,
)
from music_transfer.platforms.tidal import TidalAdapter, TidalLibraryClient

from tests.support import FakePlatformAdapter

_LOGGER = logging.getLogger("test.partial_export")


def new_account(identifier: str = "acc-1", platform: Platform = Platform.TIDAL) -> Account:
    return Account.create(platform, identifier, f"User {identifier}")


class MockVideo:
    def __init__(self, identifier: str, title: str) -> None:
        self.id = identifier
        self.title = title


class CallSpySession:
    """Mock TIDAL session tracking provider read calls across all library sections."""

    def __init__(self, *, fail_sections: set[str] | None = None) -> None:
        self.fail_sections = fail_sections or set()
        self.calls: dict[str, int] = {
            "tracks": 0,
            "albums": 0,
            "artists": 0,
            "videos": 0,
            "mixes": 0,
            "folders": 0,
            "folder_items": 0,
            "playlists": 0,
            "playlist_items": 0,
        }

        # Mock user identity
        self.user = SimpleNamespace(
            id="tidal_user_123",
            username="test_user",
            first_name="Test",
            last_name="User",
        )

        # Raw mock items
        self.raw_track_1 = SimpleNamespace(
            id="track_101",
            name="Track 101",
            duration=200,
            isrc="US101",
            artist=SimpleNamespace(id="art_1", name="Artist 1"),
            album=SimpleNamespace(id="alb_1", name="Album 1"),
        )
        self.raw_track_2 = SimpleNamespace(
            id="track_102",
            name="Track 102",
            duration=180,
            isrc="US102",
            artist=SimpleNamespace(id="art_2", name="Artist 2"),
            album=SimpleNamespace(id="alb_2", name="Album 2"),
        )
        self.raw_album = SimpleNamespace(
            id="alb_1",
            name="Album 1",
            duration=1200,
            artists=[SimpleNamespace(id="art_1", name="Artist 1")],
        )
        self.raw_artist = SimpleNamespace(
            id="art_1",
            name="Artist 1",
        )
        self.raw_video = MockVideo("vid_1", "Video 1")
        self.raw_mix = SimpleNamespace(
            id="mix_1",
            title="Mix 1",
        )
        self.raw_folder = SimpleNamespace(
            id="fld_1",
            name="Folder 1",
            items=self._folder_items_getter,
        )

        self.raw_playlist = SimpleNamespace(
            id="pl_1",
            name="Playlist 1",
            description="A test playlist",
            creator=SimpleNamespace(id="tidal_user_123"),
            items=self._playlist_items_getter,
        )

        # Wire favorites
        self.user.favorites = SimpleNamespace(
            tracks=self._tracks_getter,
            albums=self._albums_getter,
            artists=self._artists_getter,
            videos=self._videos_getter,
            mixes=self._mixes_getter,
            playlist_folders=self._folders_getter,
        )
        self.user.playlist_and_favorite_playlists = self._playlists_getter

    def _tracks_getter(self, limit: int = 50, offset: int = 0) -> list[Any]:
        self.calls["tracks"] += 1
        if "tracks" in self.fail_sections:
            raise RuntimeError("simulated tracks failure")
        if offset == 0:
            return [self.raw_track_1, self.raw_track_2]
        return []

    def _albums_getter(self, limit: int = 50, offset: int = 0) -> list[Any]:
        self.calls["albums"] += 1
        if "albums" in self.fail_sections:
            raise RuntimeError("simulated albums failure")
        if offset == 0:
            return [self.raw_album]
        return []

    def _artists_getter(self, limit: int = 50, offset: int = 0) -> list[Any]:
        self.calls["artists"] += 1
        if "artists" in self.fail_sections:
            raise RuntimeError("simulated artists failure")
        if offset == 0:
            return [self.raw_artist]
        return []

    def _videos_getter(self, limit: int = 50, offset: int = 0) -> list[Any]:
        self.calls["videos"] += 1
        if "videos" in self.fail_sections:
            raise RuntimeError("simulated videos failure")
        if offset == 0:
            return [self.raw_video]
        return []

    def _mixes_getter(self, limit: int = 50, offset: int = 0) -> list[Any]:
        self.calls["mixes"] += 1
        if "mixes" in self.fail_sections:
            raise RuntimeError("simulated mixes failure")
        if offset == 0:
            return [self.raw_mix]
        return []

    def _folders_getter(
        self, limit: int = 50, offset: int = 0, parent_folder_id: str = "root"
    ) -> list[Any]:
        self.calls["folders"] += 1
        if "folders" in self.fail_sections:
            raise RuntimeError("simulated folders failure")
        if offset == 0 and parent_folder_id == "root":
            return [self.raw_folder]
        return []

    def _folder_items_getter(self, limit: int = 50, offset: int = 0) -> list[Any]:
        self.calls["folder_items"] += 1
        if offset == 0:
            return [self.raw_playlist]
        return []

    def _playlists_getter(self, limit: int = 50, offset: int = 0) -> list[Any]:
        self.calls["playlists"] += 1
        if "playlists" in self.fail_sections:
            raise RuntimeError("simulated playlists failure")
        if offset == 0:
            return [self.raw_playlist]
        return []

    def playlist(self, playlist_id: str) -> Any:
        return self.raw_playlist

    def _playlist_items_getter(self, limit: int = 50, offset: int = 0) -> list[Any]:
        self.calls["playlist_items"] += 1
        if "playlist_items" in self.fail_sections:
            raise RuntimeError("simulated playlist_items failure")
        if offset == 0:
            return [self.raw_track_1, self.raw_track_2, self.raw_track_1]
        return []


class TransferServiceSectionSelectionTest(unittest.TestCase):
    """TransferService.analyze passes exact snapshot sections derived from ContentType."""

    def _service(self) -> TransferService:
        root = Path(tempfile.mkdtemp())
        return TransferService(
            JsonTransferJobRepository(root), JsonTransferItemRepository(root)
        )

    def test_transfer_service_passes_requested_snapshot_sections_to_source(self) -> None:
        """Verify TransferService derives correct sections for every content combination."""

        class SectionSpyAdapter(FakePlatformAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.received_sections: tuple[str, ...] | None = None

            def export_library(self, sections=None, progress=None) -> LibrarySnapshot:
                self.received_sections = sections
                return super().export_library(sections=sections, progress=progress)

        service = self._service()
        src_account = new_account("src-1")
        dst_account = new_account("dst-1")

        scenarios = [
            ((ContentType.LIKED_TRACKS,), ("tracks",)),
            ((ContentType.SAVED_ALBUMS,), ("albums",)),
            ((ContentType.FOLLOWED_ARTISTS,), ("artists",)),
            ((ContentType.PLAYLISTS,), ("folders", "playlists")),
            ((ContentType.LIKED_TRACKS, ContentType.SAVED_ALBUMS), ("albums", "tracks")),
            (
                (
                    ContentType.LIKED_TRACKS,
                    ContentType.SAVED_ALBUMS,
                    ContentType.FOLLOWED_ARTISTS,
                    ContentType.PLAYLISTS,
                ),
                ("albums", "artists", "folders", "playlists", "tracks"),
            ),
        ]

        for content, expected_sections in scenarios:
            with self.subTest(content=content):
                source = SectionSpyAdapter()
                destination = FakePlatformAdapter()
                job = service.create_job(src_account, dst_account, content=content)
                service.analyze(job, source, destination)
                self.assertEqual(source.received_sections, expected_sections)

        # When source does NOT support read_folders, only playlists is exported
        no_fld_caps = PlatformCapabilities(
            platform=Platform.TIDAL,
            read_playlists=True,
            read_folders=False,
        )
        source_no_fld = SectionSpyAdapter()
        source_no_fld._capabilities = no_fld_caps
        destination = FakePlatformAdapter()
        job = service.create_job(src_account, dst_account, content=(ContentType.PLAYLISTS,))
        service.analyze(job, source_no_fld, destination)
        self.assertEqual(source_no_fld.received_sections, ("playlists",))

    def test_content_sections_derivation_from_engine_specs(self) -> None:
        """Verify content_sections derives exactly the registered snapshot_sections."""

        self.assertEqual(content_sections((ContentType.LIKED_TRACKS,)), ("tracks",))
        self.assertEqual(content_sections((ContentType.SAVED_ALBUMS,)), ("albums",))
        self.assertEqual(content_sections((ContentType.FOLLOWED_ARTISTS,)), ("artists",))
        self.assertEqual(content_sections((ContentType.PLAYLISTS,)), ("playlists",))
        self.assertEqual(
            content_sections((ContentType.LIKED_TRACKS, ContentType.SAVED_ALBUMS)),
            ("albums", "tracks"),
        )


class TidalClientSelectiveExportTest(unittest.TestCase):
    """TidalLibraryClient selective export contract."""

    def setUp(self) -> None:
        self.session = CallSpySession()
        self.client = TidalLibraryClient(self.session, _LOGGER)

    def test_tidal_export_library_none_preserves_full_export(self) -> None:
        """sections=None (and default) executes all 7 section producers."""

        snapshot = self.client.export_library()
        self.assertGreater(self.session.calls["tracks"], 0)
        self.assertGreater(self.session.calls["albums"], 0)
        self.assertGreater(self.session.calls["artists"], 0)
        self.assertGreater(self.session.calls["videos"], 0)
        self.assertGreater(self.session.calls["mixes"], 0)
        self.assertGreater(self.session.calls["folders"], 0)
        self.assertGreater(self.session.calls["playlists"], 0)
        self.assertEqual(len(snapshot.tracks), 2)
        self.assertEqual(len(snapshot.albums), 1)
        self.assertEqual(len(snapshot.artists), 1)
        self.assertEqual(len(snapshot.playlists), 1)
        self.assertEqual(snapshot.incomplete_sections, [])

    def test_tidal_export_library_empty_selection_reads_no_content_sections(self) -> None:
        """sections=() reads zero content sections, preserving valid snapshot structure."""

        snapshot = self.client.export_library(sections=())
        for section, count in self.session.calls.items():
            self.assertEqual(count, 0, f"Expected 0 calls for {section}, got {count}")
        self.assertEqual(snapshot.tracks, [])
        self.assertEqual(snapshot.albums, [])
        self.assertEqual(snapshot.artists, [])
        self.assertEqual(snapshot.videos, [])
        self.assertEqual(snapshot.mixes, [])
        self.assertEqual(snapshot.folders, [])
        self.assertEqual(snapshot.playlists, [])
        self.assertEqual(snapshot.incomplete_sections, [])
        self.assertFalse(snapshot.is_partial)
        self.assertEqual(snapshot.account.account_id, "tidal_user_123")

    def test_tidal_export_library_tracks_only_skips_all_unrequested_sections(self) -> None:
        """sections=('tracks',) calls only tracks provider."""

        snapshot = self.client.export_library(sections=("tracks",))
        self.assertGreater(self.session.calls["tracks"], 0)
        self.assertEqual(self.session.calls["albums"], 0)
        self.assertEqual(self.session.calls["artists"], 0)
        self.assertEqual(self.session.calls["videos"], 0)
        self.assertEqual(self.session.calls["mixes"], 0)
        self.assertEqual(self.session.calls["folders"], 0)
        self.assertEqual(self.session.calls["playlists"], 0)
        self.assertEqual(self.session.calls["playlist_items"], 0)
        self.assertEqual(len(snapshot.tracks), 2)
        self.assertEqual(snapshot.albums, [])
        self.assertEqual(snapshot.artists, [])
        self.assertEqual(snapshot.playlists, [])
        self.assertEqual(snapshot.incomplete_sections, [])

    def test_tidal_export_library_albums_only_skips_unrequested_sections(self) -> None:
        """sections=('albums',) calls only albums provider."""

        snapshot = self.client.export_library(sections=("albums",))
        self.assertGreater(self.session.calls["albums"], 0)
        self.assertEqual(self.session.calls["tracks"], 0)
        self.assertEqual(self.session.calls["artists"], 0)
        self.assertEqual(self.session.calls["videos"], 0)
        self.assertEqual(self.session.calls["mixes"], 0)
        self.assertEqual(self.session.calls["folders"], 0)
        self.assertEqual(self.session.calls["playlists"], 0)
        self.assertEqual(len(snapshot.albums), 1)
        self.assertEqual(snapshot.tracks, [])
        self.assertEqual(snapshot.incomplete_sections, [])

    def test_tidal_export_library_artists_only_skips_unrequested_sections(self) -> None:
        """sections=('artists',) calls only artists provider."""

        snapshot = self.client.export_library(sections=("artists",))
        self.assertGreater(self.session.calls["artists"], 0)
        self.assertEqual(self.session.calls["tracks"], 0)
        self.assertEqual(self.session.calls["albums"], 0)
        self.assertEqual(self.session.calls["videos"], 0)
        self.assertEqual(self.session.calls["mixes"], 0)
        self.assertEqual(self.session.calls["folders"], 0)
        self.assertEqual(self.session.calls["playlists"], 0)
        self.assertEqual(len(snapshot.artists), 1)
        self.assertEqual(snapshot.tracks, [])
        self.assertEqual(snapshot.incomplete_sections, [])

    def test_tidal_export_library_mixed_selection_reads_exact_union(self) -> None:
        """sections=('tracks', 'albums') calls only tracks and albums (no duplicates)."""

        snapshot = self.client.export_library(sections=("tracks", "albums", "tracks"))
        self.assertGreater(self.session.calls["tracks"], 0)
        self.assertGreater(self.session.calls["albums"], 0)
        self.assertEqual(self.session.calls["artists"], 0)
        self.assertEqual(self.session.calls["videos"], 0)
        self.assertEqual(self.session.calls["mixes"], 0)
        self.assertEqual(self.session.calls["folders"], 0)
        self.assertEqual(self.session.calls["playlists"], 0)
        self.assertEqual(len(snapshot.tracks), 2)
        self.assertEqual(len(snapshot.albums), 1)
        self.assertEqual(snapshot.incomplete_sections, [])

    def test_tidal_export_library_unknown_section_fails_before_provider_reads(self) -> None:
        """Unknown section name fails closed with UnsupportedCapabilityError before any read."""

        with self.assertRaises(UnsupportedCapabilityError) as ctx:
            self.client.export_library(sections=("tracks", "invalid_section_name"))

        self.assertEqual(ctx.exception.capability, "invalid_section_name")
        for section, count in self.session.calls.items():
            self.assertEqual(count, 0, f"Expected 0 calls for {section}, got {count}")

    def test_unrequested_sections_are_not_marked_incomplete(self) -> None:
        """Intentionally unrequested sections do not appear in incomplete_sections."""

        snapshot = self.client.export_library(sections=("tracks",))
        self.assertEqual(snapshot.incomplete_sections, [])
        self.assertFalse(snapshot.is_partial)

    def test_requested_failed_section_is_marked_incomplete(self) -> None:
        """A requested section that fails is isolated and marked incomplete."""

        session = CallSpySession(fail_sections={"albums"})
        client = TidalLibraryClient(session, _LOGGER)
        snapshot = client.export_library(sections=("tracks", "albums"))

        self.assertEqual(len(snapshot.tracks), 2)
        self.assertEqual(snapshot.albums, [])
        self.assertEqual(snapshot.incomplete_sections, ["albums"])
        self.assertTrue(snapshot.is_partial)

    def test_progress_is_emitted_only_for_requested_sections(self) -> None:
        """Progress callbacks only emit events for requested sections."""

        emitted: list[tuple[str, int, int]] = []

        def progress(section: str, current: int, total: int) -> None:
            emitted.append((section, current, total))

        self.client.export_library(sections=("tracks",), progress=progress)
        self.assertTrue(all(item[0] == "tracks" for item in emitted))
        self.assertFalse(any(item[0] in {"albums", "artists", "playlists", "folders", "videos", "mixes"} for item in emitted))


    def test_tidal_adapter_forwards_sections_to_client(self) -> None:
        """TidalAdapter passes sections parameter directly through to TidalLibraryClient."""

        adapter = TidalAdapter(self.client)
        snapshot = adapter.export_library(sections=("tracks",))
        self.assertGreater(self.session.calls["tracks"], 0)
        self.assertEqual(self.session.calls["albums"], 0)
        self.assertEqual(self.session.calls["artists"], 0)
        self.assertEqual(self.session.calls["playlists"], 0)
        self.assertEqual(len(snapshot.tracks), 2)


class PlaylistSelectiveExportTest(unittest.TestCase):
    """Playlist selective export preserves order, duplicates, and matches full export."""

    def test_selective_playlist_export_preserves_item_order(self) -> None:
        """Playlist track order is preserved exactly in selective export."""

        session = CallSpySession()
        client = TidalLibraryClient(session, _LOGGER)
        snapshot = client.export_library(sections=("playlists",))

        self.assertEqual(len(snapshot.playlists), 1)
        playlist = snapshot.playlists[0]
        self.assertEqual(len(playlist.tracks), 3)
        self.assertEqual(playlist.tracks[0].track.source_id, "track_101")
        self.assertEqual(playlist.tracks[1].track.source_id, "track_102")
        self.assertEqual(playlist.tracks[2].track.source_id, "track_101")
        self.assertEqual([item.position for item in playlist.tracks], [0, 1, 2])

    def test_selective_playlist_export_preserves_duplicates(self) -> None:
        """Duplicate playlist entries are preserved with distinct positions."""

        session = CallSpySession()
        client = TidalLibraryClient(session, _LOGGER)
        snapshot = client.export_library(sections=("playlists",))

        playlist = snapshot.playlists[0]
        self.assertEqual(playlist.tracks[0].track.source_id, playlist.tracks[2].track.source_id)
        self.assertNotEqual(playlist.tracks[0].position, playlist.tracks[2].position)

    def test_selective_playlist_export_matches_full_export_for_requested_playlist_data(self) -> None:
        """Selective playlist export produces identical playlist objects to full export."""

        session_full = CallSpySession()
        client_full = TidalLibraryClient(session_full, _LOGGER)
        full_snapshot = client_full.export_library(sections=None)

        session_selective = CallSpySession()
        client_selective = TidalLibraryClient(session_selective, _LOGGER)
        selective_snapshot = client_selective.export_library(sections=("playlists",))

        self.assertEqual(len(full_snapshot.playlists), len(selective_snapshot.playlists))
        full_pl = full_snapshot.playlists[0]
        sel_pl = selective_snapshot.playlists[0]

        self.assertEqual(full_pl.source_id, sel_pl.source_id)
        self.assertEqual(full_pl.name, sel_pl.name)
        self.assertEqual(full_pl.description, sel_pl.description)
        self.assertEqual(len(full_pl.tracks), len(sel_pl.tracks))
        for t_full, t_sel in zip(full_pl.tracks, sel_pl.tracks, strict=True):
            self.assertEqual(t_full.position, t_sel.position)
            self.assertEqual(t_full.track.source_id, t_sel.track.source_id)
            self.assertEqual(t_full.track.title, t_sel.track.title)


class EndToEndProviderReadRegressionTest(unittest.TestCase):
    """End-to-end regressions verifying actual provider reads through TransferService -> TidalAdapter."""

    def _service(self) -> TransferService:
        root = Path(tempfile.mkdtemp())
        return TransferService(
            JsonTransferJobRepository(root), JsonTransferItemRepository(root)
        )

    def test_track_only_analysis_does_not_call_unrelated_tidal_library_apis(self) -> None:
        """LIKED_TRACKS transfer reads only tracks library API."""

        session = CallSpySession()
        client = TidalLibraryClient(session, _LOGGER)
        source = TidalAdapter(client)
        destination = FakePlatformAdapter()

        service = self._service()
        job = service.create_job(
            new_account("src-tidal", platform=Platform.TIDAL),
            new_account("dst-tidal", platform=Platform.TIDAL),
            content=(ContentType.LIKED_TRACKS,),
        )

        service.analyze(job, source, destination)

        self.assertGreater(session.calls["tracks"], 0)
        self.assertEqual(session.calls["albums"], 0)
        self.assertEqual(session.calls["artists"], 0)
        self.assertEqual(session.calls["videos"], 0)
        self.assertEqual(session.calls["mixes"], 0)
        self.assertEqual(session.calls["folders"], 0)
        self.assertEqual(session.calls["folder_items"], 0)
        self.assertEqual(session.calls["playlists"], 0)
        self.assertEqual(session.calls["playlist_items"], 0)

    def test_album_only_analysis_does_not_call_unrelated_tidal_library_apis(self) -> None:
        """SAVED_ALBUMS transfer reads only albums library API."""

        session = CallSpySession()
        client = TidalLibraryClient(session, _LOGGER)
        source = TidalAdapter(client)
        destination = FakePlatformAdapter()

        service = self._service()
        job = service.create_job(
            new_account("src-tidal", platform=Platform.TIDAL),
            new_account("dst-tidal", platform=Platform.TIDAL),
            content=(ContentType.SAVED_ALBUMS,),
        )

        service.analyze(job, source, destination)

        self.assertGreater(session.calls["albums"], 0)
        self.assertEqual(session.calls["tracks"], 0)
        self.assertEqual(session.calls["artists"], 0)
        self.assertEqual(session.calls["videos"], 0)
        self.assertEqual(session.calls["mixes"], 0)
        self.assertEqual(session.calls["folders"], 0)
        self.assertEqual(session.calls["playlists"], 0)
        self.assertEqual(session.calls["playlist_items"], 0)

    def test_artist_only_analysis_does_not_call_unrelated_tidal_library_apis(self) -> None:
        """FOLLOWED_ARTISTS transfer reads only artists library API."""

        session = CallSpySession()
        client = TidalLibraryClient(session, _LOGGER)
        source = TidalAdapter(client)
        destination = FakePlatformAdapter()

        service = self._service()
        job = service.create_job(
            new_account("src-tidal", platform=Platform.TIDAL),
            new_account("dst-tidal", platform=Platform.TIDAL),
            content=(ContentType.FOLLOWED_ARTISTS,),
        )

        service.analyze(job, source, destination)

        self.assertGreater(session.calls["artists"], 0)
        self.assertEqual(session.calls["tracks"], 0)
        self.assertEqual(session.calls["albums"], 0)
        self.assertEqual(session.calls["videos"], 0)
        self.assertEqual(session.calls["mixes"], 0)
        self.assertEqual(session.calls["folders"], 0)
        self.assertEqual(session.calls["playlists"], 0)
        self.assertEqual(session.calls["playlist_items"], 0)

    def test_playlist_only_analysis_does_not_read_liked_library_sections(self) -> None:
        """PLAYLISTS transfer reads playlists, playlist items, and folders (no liked libraries)."""

        session = CallSpySession()
        client = TidalLibraryClient(session, _LOGGER)
        source = TidalAdapter(client)
        destination = FakePlatformAdapter()

        service = self._service()
        job = service.create_job(
            new_account("src-tidal", platform=Platform.TIDAL),
            new_account("dst-tidal", platform=Platform.TIDAL),
            content=(ContentType.PLAYLISTS,),
        )

        service.analyze(job, source, destination)

        self.assertGreater(session.calls["playlists"], 0)
        self.assertGreater(session.calls["playlist_items"], 0)
        self.assertGreater(session.calls["folders"], 0)
        self.assertEqual(session.calls["tracks"], 0)
        self.assertEqual(session.calls["albums"], 0)
        self.assertEqual(session.calls["artists"], 0)
        self.assertEqual(session.calls["videos"], 0)
        self.assertEqual(session.calls["mixes"], 0)

    def test_mixed_selection_analysis_reads_exact_union_of_required_sections(self) -> None:
        """Mixed LIKED_TRACKS + SAVED_ALBUMS reads exactly tracks and albums."""

        session = CallSpySession()
        client = TidalLibraryClient(session, _LOGGER)
        source = TidalAdapter(client)
        destination = FakePlatformAdapter()

        service = self._service()
        job = service.create_job(
            new_account("src-tidal", platform=Platform.TIDAL),
            new_account("dst-tidal", platform=Platform.TIDAL),
            content=(ContentType.LIKED_TRACKS, ContentType.SAVED_ALBUMS),
        )

        service.analyze(job, source, destination)

        self.assertGreater(session.calls["tracks"], 0)
        self.assertGreater(session.calls["albums"], 0)
        self.assertEqual(session.calls["artists"], 0)
        self.assertEqual(session.calls["videos"], 0)
        self.assertEqual(session.calls["mixes"], 0)
        self.assertEqual(session.calls["folders"], 0)
        self.assertEqual(session.calls["playlists"], 0)
        self.assertEqual(session.calls["playlist_items"], 0)


if __name__ == "__main__":
    unittest.main()

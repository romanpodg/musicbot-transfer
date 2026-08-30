"""Universal domain model tests and the TIDAL mapping round-trip.

Two things are pinned here:

1. the universal models are the single source of truth for what a transfer
   item *is*, regardless of platform;
2. mapping a TIDAL object into those models preserves identity, order, and the
   original metadata - normalization must never destroy the original.
"""

from __future__ import annotations

import unittest
from typing import Any

from music_transfer.core.domain import (
    Account,
    AccountProfile,
    Album,
    Artist,
    LibrarySnapshot,
    Playlist,
    PlaylistItem,
    Track,
    TransferItem,
    TransferJob,
)
from music_transfer.core.enums import ContentType, EntityType, Platform
from music_transfer.platforms.tidal.mapper import (
    album_from_tidal,
    artist_from_tidal,
    date_value,
    number_value,
    playlist_from_tidal,
    playlist_item_from_tidal,
    text_value,
    track_from_tidal,
)
from tests.support import album, artist, playlist, track


class FakeTidal:
    """A duck-typed stand-in for a ``tidalapi`` object."""

    def __init__(self, **attributes: Any) -> None:
        for key, value in attributes.items():
            setattr(self, key, value)


class UniversalModelTests(unittest.TestCase):
    """The models carry identity, explicit fields, and untouched metadata."""

    def test_track_keeps_source_identity(self) -> None:
        """A track knows which platform and id it came from."""

        item = track("Song", identifier="42")
        self.assertEqual(item.source_platform, Platform.TIDAL)
        self.assertEqual(item.source_id, "42")

    def test_original_metadata_survives_normalization(self) -> None:
        """normalizing for comparison never rewrites the stored title."""

        item = track("Song (Remastered 2011) [Explicit]", identifier="1")
        self.assertEqual(item.title, "Song (Remastered 2011) [Explicit]")

    def test_metadata_dict_carries_platform_extras(self) -> None:
        """Unknown platform fields land in ``metadata`` instead of being dropped."""

        item = track("Song", identifier="1")
        item.metadata["tidal_audio_quality"] = "HI_RES"
        self.assertEqual(item.metadata["tidal_audio_quality"], "HI_RES")

    def test_track_serialization_round_trip(self) -> None:
        """A track survives ``as_dict``/``from_dict`` unchanged."""

        item = track("Song", (artist("A"), artist("B")), identifier="1", isrc="USRC17607839")
        restored = Track.from_dict(item.as_dict())
        self.assertEqual(restored.title, item.title)
        self.assertEqual(restored.isrc, item.isrc)
        self.assertEqual([a.name for a in restored.artists], ["A", "B"])

    def test_album_round_trip(self) -> None:
        """An album survives serialization with its artists."""

        original = album("Album", (artist("A"),))
        restored = Album.from_dict(original.as_dict())
        self.assertEqual(restored.title, "Album")
        self.assertEqual([a.name for a in restored.artists], ["A"])

    def test_playlist_round_trip_preserves_items(self) -> None:
        """A playlist survives serialization with order and duplicates intact."""

        repeated = track("A", identifier="a")
        original = playlist("P", [repeated, track("B", identifier="b"), repeated])
        restored = Playlist.from_dict(original.as_dict())
        self.assertEqual(restored.ordered_track_ids(), ["a", "b", "a"])

    def test_artist_requires_a_source_id(self) -> None:
        """An artist without an identifier is rejected loudly."""

        with self.assertRaises(ValueError):
            Artist(source_platform=Platform.TIDAL, source_id="", name="Nobody")

    def test_account_identity_is_platform_scoped(self) -> None:
        """Two TIDAL accounts differ; the same account matches itself."""

        first = Account.create(Platform.TIDAL, "1", "First")
        second = Account.create(Platform.TIDAL, "2", "Second")
        self.assertFalse(first.same_identity(second))
        self.assertTrue(first.same_identity(Account.create(Platform.TIDAL, "1")))
        # The same numeric id on another platform is a different account.
        self.assertFalse(
            first.same_identity(Account.create(Platform.SPOTIFY, "1", "Spotify"))
        )

    def test_account_carries_no_credentials(self) -> None:
        """Serialization contains an auth reference, never a token."""

        account = Account.create(
            Platform.TIDAL, "1", "Roman", auth_reference="keyring:tidal:source"
        )
        payload = account.as_dict()
        self.assertEqual(payload["auth_reference"], "keyring:tidal:source")
        self.assertNotIn("token", payload)
        self.assertNotIn("password", payload)

    def test_snapshot_reports_partial_reads(self) -> None:
        """An incomplete section is reported, never hidden."""

        snapshot = LibrarySnapshot(
            account=AccountProfile("1", "Roman", Platform.TIDAL),
            platform=Platform.TIDAL,
            incomplete_sections=("playlists",),
        )
        self.assertTrue(snapshot.is_partial)
        self.assertEqual(snapshot.incomplete_sections, ("playlists",))

    def test_snapshot_counts_every_section(self) -> None:
        """Counts cover every section a transfer can request."""

        snapshot = LibrarySnapshot(
            account=AccountProfile("1", "Roman", Platform.TIDAL),
            platform=Platform.TIDAL,
            tracks=[track("A", identifier="a")],
            albums=[album("B")],
        )
        counts = snapshot.counts()
        self.assertEqual(counts["tracks"], 1)
        self.assertEqual(counts["albums"], 1)
        self.assertEqual(counts["playlists"], 0)


class TidalMappingTests(unittest.TestCase):
    """TIDAL objects map onto the universal models without loss."""

    def test_track_mapping(self) -> None:
        """A TIDAL track maps with seconds converted to milliseconds."""

        raw = FakeTidal(
            id="123",
            name="Take On Me",
            isrc="USRC17607839",
            duration=225,
            explicit=True,
            artists=[FakeTidal(id="9", name="a-ha")],
            album=FakeTidal(id="55", name="Hunting High and Low"),
        )
        mapped = track_from_tidal(raw)
        self.assertEqual(mapped.source_id, "123")
        self.assertEqual(mapped.title, "Take On Me")
        self.assertEqual(mapped.isrc, "USRC17607839")
        self.assertEqual(mapped.duration_ms, 225_000)
        self.assertTrue(mapped.explicit)
        self.assertEqual(mapped.artist_names, ["a-ha"])
        self.assertEqual(mapped.album_title, "Hunting High and Low")

    def test_artist_mapping(self) -> None:
        """A TIDAL artist maps onto the universal artist."""

        mapped = artist_from_tidal(FakeTidal(id="9", name="a-ha"))
        self.assertEqual(mapped.source_id, "9")
        self.assertEqual(mapped.name, "a-ha")
        self.assertEqual(mapped.source_platform, Platform.TIDAL)

    def test_album_mapping(self) -> None:
        """A TIDAL album maps with its artists attached."""

        mapped = album_from_tidal(
            FakeTidal(id="55", name="Hunting High and Low", artists=[FakeTidal(id="9", name="a-ha")])
        )
        self.assertEqual(mapped.source_id, "55")
        self.assertEqual(mapped.artist_names, ["a-ha"])

    def test_playlist_mapping(self) -> None:
        """A TIDAL playlist maps with its items in order."""

        items = [
            playlist_item_from_tidal(FakeTidal(id=str(index)), index)
            for index in range(1, 4)
        ]
        mapped = playlist_from_tidal(
            FakeTidal(id="pl-1", name="Road Trip"), items=items
        )
        self.assertEqual(mapped.source_id, "pl-1")
        self.assertEqual(mapped.name, "Road Trip")
        self.assertEqual([item.position for item in mapped.tracks], [1, 2, 3])

    def test_playlist_item_mapping(self) -> None:
        """A playlist item keeps its position, which is what preserves order."""

        mapped = playlist_item_from_tidal(FakeTidal(id="77"), 5)
        self.assertEqual(mapped.position, 5)
        self.assertEqual(mapped.track.source_id, "77")

    def test_value_extractors_tolerate_missing_fields(self) -> None:
        """Extractors return an empty value instead of raising on absent data.

        A missing field is normal for a partially populated API response; the
        mapper degrades to "unknown" rather than failing the whole export.
        """

        self.assertFalse(text_value(FakeTidal(), "name"))
        self.assertIsNone(number_value(FakeTidal(), "duration"))
        self.assertIsNone(date_value(FakeTidal(), "created"))

    def test_duration_conversion_round_trip(self) -> None:
        """Milliseconds in the model correspond to the TIDAL seconds field."""

        mapped = track_from_tidal(FakeTidal(id="1", name="X", duration=181))
        self.assertEqual(mapped.duration_seconds, 181)
        self.assertEqual(mapped.duration_ms, 181_000)

    def test_missing_id_is_rejected(self) -> None:
        """An object without an id cannot become a transfer item."""

        with self.assertRaises(Exception):
            track_from_tidal(FakeTidal(name="No Id"))


class TransferObjectTests(unittest.TestCase):
    """Jobs and items are durable, typed, and explicit about failure."""

    def test_job_records_route_and_content(self) -> None:
        """A job records where it goes and what it carries."""

        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            requested_content=(ContentType.LIKED_TRACKS, ContentType.PLAYLISTS),
        )
        self.assertEqual(job.source_platform, Platform.TIDAL)
        self.assertEqual(job.destination_platform, Platform.SPOTIFY)
        self.assertEqual(
            job.requested_content, (ContentType.LIKED_TRACKS, ContentType.PLAYLISTS)
        )

    def test_job_serialization_round_trip(self) -> None:
        """A job survives persistence."""

        job = TransferJob.create(Platform.TIDAL, Platform.TIDAL, user_id="u1")
        restored = TransferJob.from_dict(job.as_dict())
        self.assertEqual(restored.id, job.id)
        self.assertEqual(restored.status, job.status)

    def test_item_records_container_for_playlist_entries(self) -> None:
        """A playlist entry knows its parent playlist."""

        item = TransferItem.create(
            "job-1",
            EntityType.PLAYLIST_ITEM,
            Platform.TIDAL,
            "track-1",
            Platform.SPOTIFY,
            container_source_id="playlist-1",
        )
        self.assertEqual(item.container_source_id, "playlist-1")

    def test_item_round_trip_preserves_status(self) -> None:
        """An item's durable status survives a restart."""

        item = TransferItem.create(
            "job-1", EntityType.TRACK, Platform.TIDAL, "1", Platform.SPOTIFY
        )
        restored = TransferItem.from_dict(item.as_dict())
        self.assertEqual(restored.status, item.status)

    def test_playlist_item_is_distinct_from_a_track(self) -> None:
        """A playlist entry models position, so it is not just a track."""

        entry = PlaylistItem(position=1, track=track("A", identifier="a"))
        self.assertEqual(entry.position, 1)
        self.assertIsInstance(entry.track, Track)


if __name__ == "__main__":
    unittest.main()

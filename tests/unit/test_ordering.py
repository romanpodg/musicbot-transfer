"""Ordering tests: logical order versus destination insertion order.

The specification requires these two concepts to stay separate:

* **logical order** is what the user asked for (newest first, alphabetical...);
* **insertion order** is what the destination does when you append or prepend.

TIDAL prepends new playlist items, so writing "A, B, C" in that order produces
"C, B, A" on screen unless the write order is compensated.  ``preserve_visible_order``
exists precisely because that compensation is a *choice*: matching the source's
visible order costs an extra assumption about the destination, and the default
keeps the proven legacy behaviour.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import UTC, datetime

from music_transfer.core.domain import Playlist, Track
from music_transfer.core.enums import InsertionBehavior, OrderingMode
from music_transfer.core.transfer.ordering import (
    apply_logical_order,
    restore_positions,
    to_write_order,
)

from tests.support import album, artist, playlist, track


@dataclass(frozen=True, slots=True)
class Row:
    """A stand-in for any orderable transfer item."""

    title: str
    artist_name: str = ""
    album_title: str = ""
    date_added: datetime | None = None


def _at(day: int) -> datetime:
    """Return a fixed UTC timestamp for ``day``."""

    return datetime(2024, 1, day, tzinfo=UTC)


ACCESSORS = {
    "date_added": lambda row: row.date_added,
    "title": lambda row: row.title,
    "artist": lambda row: row.artist_name,
    "album": lambda row: row.album_title,
}


class LogicalOrderTests(unittest.TestCase):
    """``apply_logical_order`` implements what the user asked for."""

    def test_newest_first(self) -> None:
        """Newest-added items come first."""

        rows = [Row("a", date_added=_at(1)), Row("b", date_added=_at(3)), Row("c", date_added=_at(2))]
        ordered = apply_logical_order(rows, OrderingMode.DATE_ADDED_NEWEST_FIRST, **ACCESSORS)
        self.assertEqual([row.title for row in ordered], ["b", "c", "a"])

    def test_oldest_first(self) -> None:
        """Oldest-added items come first."""

        rows = [Row("a", date_added=_at(3)), Row("b", date_added=_at(1)), Row("c", date_added=_at(2))]
        ordered = apply_logical_order(rows, OrderingMode.DATE_ADDED_OLDEST_FIRST, **ACCESSORS)
        self.assertEqual([row.title for row in ordered], ["b", "c", "a"])

    def test_alphabetical(self) -> None:
        """Titles sort alphabetically, case-insensitively."""

        rows = [Row("banana"), Row("Apple"), Row("cherry")]
        ordered = apply_logical_order(rows, OrderingMode.ALPHABETICAL, **ACCESSORS)
        self.assertEqual([row.title for row in ordered], ["Apple", "banana", "cherry"])

    def test_by_artist(self) -> None:
        """Rows sort by artist name."""

        rows = [Row("1", artist_name="Zoe"), Row("2", artist_name="Adam"), Row("3", artist_name="Mia")]
        ordered = apply_logical_order(rows, OrderingMode.ARTIST, **ACCESSORS)
        self.assertEqual([row.artist_name for row in ordered], ["Adam", "Mia", "Zoe"])

    def test_by_album(self) -> None:
        """Rows sort by album title."""

        rows = [Row("1", album_title="Zebra"), Row("2", album_title="Alpha")]
        ordered = apply_logical_order(rows, OrderingMode.ALBUM, **ACCESSORS)
        self.assertEqual([row.album_title for row in ordered], ["Alpha", "Zebra"])

    def test_source_order_is_a_no_op(self) -> None:
        """SOURCE_ORDER preserves the source sequence exactly."""

        rows = [Row("c"), Row("a"), Row("b")]
        ordered = apply_logical_order(rows, OrderingMode.SOURCE_ORDER, **ACCESSORS)
        self.assertEqual([row.title for row in ordered], ["c", "a", "b"])

    def test_missing_sort_values_sort_last(self) -> None:
        """Items without a sort value are not dropped or crashed on."""

        rows = [Row("a", date_added=None), Row("b", date_added=_at(1))]
        ordered = apply_logical_order(rows, OrderingMode.DATE_ADDED_NEWEST_FIRST, **ACCESSORS)
        self.assertEqual([row.title for row in ordered], ["b", "a"])

    def test_sorting_is_stable(self) -> None:
        """Equal keys keep their original relative order."""

        rows = [Row("a", artist_name="X"), Row("b", artist_name="X"), Row("c", artist_name="X")]
        ordered = apply_logical_order(rows, OrderingMode.ARTIST, **ACCESSORS)
        self.assertEqual([row.title for row in ordered], ["a", "b", "c"])


class InsertionBehaviorTests(unittest.TestCase):
    """``to_write_order`` separates logical order from destination behaviour."""

    def test_append_keeps_logical_order(self) -> None:
        """An APPEND destination receives items in logical order."""

        rows = [Row("a"), Row("b"), Row("c")]
        write = to_write_order(
            rows, OrderingMode.SOURCE_ORDER, InsertionBehavior.APPEND, **ACCESSORS
        )
        self.assertEqual([row.title for row in write], ["a", "b", "c"])

    def test_prepend_is_compensated_when_asked(self) -> None:
        """A PREPEND destination is reversed *only* when the user asked for it."""

        rows = [Row("a"), Row("b"), Row("c")]
        write = to_write_order(
            rows,
            OrderingMode.SOURCE_ORDER,
            InsertionBehavior.PREPEND,
            preserve_visible_order=True,
            **ACCESSORS,
        )
        # Writing c, b, a into a prepending destination yields a, b, c on screen.
        self.assertEqual([row.title for row in write], ["c", "b", "a"])

    def test_prepend_is_not_compensated_by_default(self) -> None:
        """By default the proven behaviour is kept: write in logical order."""

        rows = [Row("a"), Row("b"), Row("c")]
        write = to_write_order(
            rows, OrderingMode.SOURCE_ORDER, InsertionBehavior.PREPEND, **ACCESSORS
        )
        self.assertEqual([row.title for row in write], ["a", "b", "c"])

    def test_ordering_applies_before_insertion_compensation(self) -> None:
        """Logical ordering happens first; compensation only reverses."""

        rows = [Row("b", date_added=_at(1)), Row("a", date_added=_at(2))]
        write = to_write_order(
            rows,
            OrderingMode.DATE_ADDED_NEWEST_FIRST,
            InsertionBehavior.PREPEND,
            preserve_visible_order=True,
            **ACCESSORS,
        )
        self.assertEqual([row.title for row in write], ["b", "a"])


class PlaylistOrderAndDuplicates(unittest.TestCase):
    """Invariant D: a playlist is a sequence, not a set."""

    def test_duplicates_are_preserved(self) -> None:
        """``A, B, A`` must stay ``A, B, A`` end to end."""

        first = track("A", identifier="a")
        second = track("B", identifier="b")
        pl = playlist("Mixed", [first, second, first])
        self.assertEqual(pl.ordered_track_ids(), ["a", "b", "a"])
        self.assertEqual(pl.track_count, 3)

    def test_duplicates_survive_source_order(self) -> None:
        """Ordering never de-duplicates a playlist."""

        first = track("A", identifier="a")
        pl = playlist("Mixed", [first, first, first])
        ordered = apply_logical_order(
            pl.tracks, OrderingMode.SOURCE_ORDER, title=lambda item: item.track.title
        )
        self.assertEqual(len(ordered), 3)

    def test_positions_are_contiguous(self) -> None:
        """Item positions reflect the sequence, including repeats."""

        first = track("A", identifier="a")
        pl = playlist("Mixed", [first, track("B", identifier="b"), first])
        self.assertEqual([item.position for item in pl.tracks], [1, 2, 3])

    def test_restore_positions_rebuilds_the_source_sequence(self) -> None:
        """Positions let resume rebuild the exact source sequence."""

        pl = playlist("P", [track("A", identifier="a"), track("B", identifier="b")])
        shuffled = list(reversed(pl.tracks))
        restored = restore_positions(shuffled, position=lambda item: item.position)
        self.assertEqual([item.track.source_id for item in restored], ["a", "b"])
        self.assertEqual([item.position for item in restored], [1, 2])

    def test_restore_positions_without_accessor_keeps_order(self) -> None:
        """Without an accessor the helper is an explicit no-op, not a guess."""

        pl = playlist("P", [track("A", identifier="a")])
        self.assertEqual(restore_positions(pl.tracks), pl.tracks)

    def test_empty_playlist(self) -> None:
        """An empty playlist is valid and reports zero items."""

        pl = playlist("Empty", [])
        self.assertEqual(pl.track_count, 0)
        self.assertEqual(pl.ordered_track_ids(), [])

    def test_playlist_is_not_a_set_of_tracks(self) -> None:
        """Two playlists containing the same tracks are still distinct."""

        shared = track("A", identifier="a")
        first = playlist("One", [shared])
        second = playlist("Two", [shared])
        self.assertNotEqual(first.source_id, second.source_id)
        self.assertEqual(first.ordered_track_ids(), second.ordered_track_ids())


class TrackSortAccessors(unittest.TestCase):
    """Domain objects expose the values ordering needs."""

    def test_track_sort_values(self) -> None:
        """A track exposes title, artist, album, and added date."""

        item = track(
            "Song",
            (artist("Zoe"),),
            album=album("Alpha"),
            identifier="1",
        )
        self.assertEqual(item.title, "Song")
        self.assertEqual(item.artist_names, ["Zoe"])
        self.assertEqual(item.primary_artist.name, "Zoe")
        self.assertEqual(item.album_title, "Alpha")
        self.assertIsInstance(item, Track)

    def test_playlist_sort_values(self) -> None:
        """A playlist is orderable by name."""

        pl = playlist("Road Trip", [])
        self.assertEqual(pl.name, "Road Trip")
        self.assertIsInstance(pl, Playlist)


if __name__ == "__main__":
    unittest.main()

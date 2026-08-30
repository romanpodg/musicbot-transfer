"""Regression tests for TIDAL offset pagination.

This file exists because of one specific bug.  The original paginator read::

    if len(page) < page_size:
        return values
    offset += len(page)

Two defects, one visible and one subtle:

1. A short but non-empty page was treated as the end of data.  TIDAL returns
   short pages in the middle of a collection (rights filtering, deduplication),
   so libraries were silently truncated.
2. The offset advanced by ``len(page)`` instead of the requested page size, so
   after a short page every subsequent request was off the page grid.

The tests below pin the corrected behaviour: only an **empty** page ends
pagination, and the offset always advances by the requested page size.
"""

from __future__ import annotations

import unittest

from music_transfer.core.errors import PaginationError
from music_transfer.platforms.tidal.pagination import (
    DEFAULT_POLICY,
    PLAYLIST_POLICY,
    PaginationPolicy,
    fetch_all,
)


def page_getter(pages: dict[int, list[object]], default: list[object] | None = None):
    """Build a fake ``getter(limit=..., offset=...)`` from an offset map.

    Offsets absent from the map return ``default`` (an empty page unless the
    test says otherwise), which is how a real endpoint behaves past the end.
    """

    calls: list[tuple[int, int]] = []

    def getter(*, limit: int, offset: int) -> list[object]:
        calls.append((limit, offset))
        return list(pages.get(offset, default if default is not None else []))

    getter.calls = calls  # type: ignore[attr-defined]
    return getter


class ShortPageIsNotTheEnd(unittest.TestCase):
    """Invariant A: a short non-empty page does not end pagination."""

    def test_100_plus_49_plus_100_equals_249(self) -> None:
        """The canonical regression: pages of 100, 49, 100, then empty.

        The 49-item page is *not* the end.  Traversal must continue to offset
        200 and collect the final 100 items, giving 249 in total.
        """

        pages = {
            0: list(range(0, 100)),
            100: list(range(100, 149)),
            200: list(range(149, 249)),
            300: [],
        }
        getter = page_getter(pages)
        policy = PaginationPolicy(page_size=100)
        values = fetch_all(getter, policy=policy, operation="test")

        self.assertEqual(len(values), 249, "short page must not end pagination")
        self.assertEqual(values[:100], list(range(0, 100)))
        self.assertEqual(values[100:149], list(range(100, 149)))
        self.assertEqual(values[149:], list(range(149, 249)))
        # Every request asked for the full page size and stayed on the grid.
        self.assertEqual(
            getter.calls,
            [(100, 0), (100, 100), (100, 200), (100, 300)],
            "offset must advance by the requested page size, not by len(page)",
        )

    def test_single_short_page_still_terminates_on_empty_page(self) -> None:
        """A genuinely final collection ends because the *next* page is empty."""

        getter = page_getter({0: [1, 2, 3], 100: []})
        values = fetch_all(getter, policy=PaginationPolicy(page_size=100))
        self.assertEqual(values, [1, 2, 3])
        self.assertEqual(getter.calls, [(100, 0), (100, 100)])

    def test_exactly_full_pages_then_empty(self) -> None:
        """Full pages behave identically: termination is still the empty page."""

        getter = page_getter({0: list(range(50)), 50: list(range(50, 100)), 100: []})
        values = fetch_all(getter, policy=PaginationPolicy(page_size=50))
        self.assertEqual(len(values), 100)
        self.assertEqual(getter.calls, [(50, 0), (50, 50), (50, 100)])

    def test_many_short_pages_are_all_collected(self) -> None:
        """Several consecutive one-item pages must all be collected."""

        pages = {offset: [offset] for offset in (0, 100, 200, 300)}
        getter = page_getter(pages)
        values = fetch_all(getter, policy=PaginationPolicy(page_size=100))
        self.assertEqual(values, [0, 100, 200, 300])

    def test_alternating_short_and_full_pages(self) -> None:
        """Alternate short/full pages, the shape that hides the original bug."""

        pages = {
            0: list(range(10)),
            100: list(range(10, 110)),
            200: list(range(110, 115)),
            300: list(range(115, 215)),
            400: [],
        }
        values = fetch_all(page_getter(pages), policy=PaginationPolicy(page_size=100))
        self.assertEqual(len(values), 215)
        self.assertEqual(values[-1], 214)

    def test_page_size_is_always_the_requested_one(self) -> None:
        """The limit passed to every request equals the policy page size."""

        getter = page_getter({0: [1], 7: []})
        fetch_all(getter, policy=PaginationPolicy(page_size=7))
        self.assertTrue(all(limit == 7 for limit, _ in getter.calls))


class PaginationSafetyBounds(unittest.TestCase):
    """Misbehaving endpoints must fail loudly, not loop or truncate silently."""

    def test_repeated_identical_page_raises(self) -> None:
        """A server that ignores ``offset`` would loop forever; it must raise."""

        getter = page_getter({}, default=[1, 2, 3])
        with self.assertRaises(PaginationError) as context:
            fetch_all(getter, policy=PaginationPolicy(page_size=10, max_pages=50))
        self.assertEqual(context.exception.code, "pagination_repeated_page")

    def test_page_limit_raises(self) -> None:
        """Exceeding ``max_pages`` raises instead of spinning indefinitely."""

        pages = {offset: [offset] for offset in range(0, 1000, 10)}
        getter = page_getter(pages)
        with self.assertRaises(PaginationError) as context:
            fetch_all(getter, policy=PaginationPolicy(page_size=10, max_pages=5))
        self.assertEqual(context.exception.code, "pagination_max_pages_exceeded")

    def test_item_limit_raises(self) -> None:
        """Exceeding ``max_items`` raises, bounding runaway growth."""

        pages = {offset: list(range(offset, offset + 10)) for offset in range(0, 200, 10)}
        with self.assertRaises(PaginationError) as context:
            fetch_all(
                page_getter(pages),
                policy=PaginationPolicy(page_size=10, max_items=25),
            )
        self.assertEqual(context.exception.code, "pagination_max_items_exceeded")

    def test_empty_first_page_returns_empty(self) -> None:
        """An empty collection is a single request, not an error."""

        getter = page_getter({0: []})
        self.assertEqual(fetch_all(getter, policy=PaginationPolicy(page_size=100)), [])
        self.assertEqual(getter.calls, [(100, 0)])

    def test_getter_exception_propagates(self) -> None:
        """A transport failure is never swallowed into a short result."""

        def getter(*, limit: int, offset: int) -> list[object]:
            raise OSError("network down")

        with self.assertRaises(OSError):
            fetch_all(getter, policy=PaginationPolicy(page_size=10))

    def test_progress_callback_reports_running_totals(self) -> None:
        """Progress reports the running total and page count after each page."""

        pages = {0: list(range(100)), 100: list(range(100, 149)), 200: []}
        seen: list[tuple[int, int]] = []
        fetch_all(
            page_getter(pages),
            policy=PaginationPolicy(page_size=100),
            progress=lambda collected, page: seen.append((collected, page)),
        )
        self.assertEqual(seen, [(100, 1), (149, 2)])


class PolicyDefaults(unittest.TestCase):
    """The shipped page sizes match the proven per-section values."""

    def test_default_page_size(self) -> None:
        """Library sections are read 50 at a time."""

        self.assertEqual(DEFAULT_POLICY.page_size, 50)

    def test_playlist_page_size(self) -> None:
        """Playlist contents are read 100 at a time."""

        self.assertEqual(PLAYLIST_POLICY.page_size, 100)

    def test_policies_have_bounds(self) -> None:
        """Both policies carry finite safety bounds."""

        for policy in (DEFAULT_POLICY, PLAYLIST_POLICY):
            with self.subTest(policy=policy):
                self.assertGreater(policy.max_pages, 0)
                self.assertGreater(policy.max_items, 0)


if __name__ == "__main__":
    unittest.main()

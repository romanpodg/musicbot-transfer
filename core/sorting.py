"""Stable sort strategies for transferring favorite tracks."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Iterable


class SortOrder(StrEnum):
    """Supported user-selected transfer ordering strategies."""

    NEWEST_FIRST = "newest_first"
    OLDEST_FIRST = "oldest_first"
    ALPHABETICAL = "alphabetical"
    ARTIST = "artist"
    ALBUM = "album"
    ORIGINAL = "original"


def sort_items(items: Iterable[dict[str, Any]], order: SortOrder) -> list[dict[str, Any]]:
    """Return a stable sorted copy of library objects.

    Missing metadata is sorted last and never causes a transfer failure.
    """

    result = list(items)
    if order is SortOrder.ORIGINAL:
        return result
    if order is SortOrder.NEWEST_FIRST:
        return sorted(result, key=_added_date_key, reverse=True)
    if order is SortOrder.OLDEST_FIRST:
        return sorted(result, key=_added_date_key)
    key_map = {
        SortOrder.ALPHABETICAL: "title",
        SortOrder.ARTIST: "artist",
        SortOrder.ALBUM: "album",
    }
    field = key_map[order]
    return sorted(result, key=lambda item: _text_key(item.get(field)))


def _added_date_key(item: dict[str, Any]) -> tuple[bool, str]:
    """Put missing dates last while retaining ISO-8601 lexical order."""

    date = item.get("added_at")
    return (date is None, str(date or ""))


def _text_key(value: Any) -> tuple[bool, str]:
    """Build a locale-neutral, case-insensitive string sort key."""

    text = str(value or "").casefold()
    return (not bool(text), text)

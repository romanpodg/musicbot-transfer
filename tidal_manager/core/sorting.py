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
    if order in {SortOrder.NEWEST_FIRST, SortOrder.OLDEST_FIRST}:
        # Do not use reverse=True on a key containing the missing flag: doing
        # so reverses that flag too and puts records without a date first.
        present = [item for item in result if _added_date(item) is not None]
        missing = [item for item in result if _added_date(item) is None]
        return sorted(
            present,
            key=lambda item: _added_date(item) or "",
            reverse=order is SortOrder.NEWEST_FIRST,
        ) + missing
    key_map = {
        SortOrder.ALPHABETICAL: "title",
        SortOrder.ARTIST: "artist",
        SortOrder.ALBUM: "album",
    }
    field = key_map[order]
    return sorted(result, key=lambda item: _text_key(_sort_value(item, field)))


def _added_date(item: dict[str, Any]) -> str | None:
    """Return the exported favourite date, accepting old backup field names."""

    value = item.get("added_at", item.get("user_date_added"))
    return str(value) if value not in (None, "") else None


def _sort_value(item: dict[str, Any], field: str) -> Any:
    """Read the display field appropriate to every supported library object."""

    if field == "title":
        return item.get("title", item.get("name"))
    if field == "artist":
        return item.get("artist", item.get("artist_name"))
    if field == "album":
        return item.get("album", item.get("album_title"))
    return item.get(field)


def _text_key(value: Any) -> tuple[bool, str]:
    """Build a locale-neutral, case-insensitive string sort key."""

    text = str(value or "").casefold()
    return (not bool(text), text)

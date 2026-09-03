"""Map ``tidalapi`` objects onto universal domain models.

This is the only module that reads provider attributes.  Keeping extraction in
one place means a ``tidalapi`` upgrade is a single-file change, and it keeps
provider shapes out of the transfer engine.

Extraction rules preserved from the proven implementation:

* the first non-empty attribute wins when a provider renames a field
  (``name``/``title``, ``available_release_date``/``release_date``);
* a missing identifier is an error, not an empty string, because downstream
  write and resume logic cannot act on an unidentifiable object;
* missing optional metadata yields ``None`` rather than raising, so one odd
  item never aborts an export.
"""

from __future__ import annotations

from typing import Any

from ...core.domain import Album, Artist, LibraryRecord, Playlist, PlaylistItem, Track
from ...core.enums import Platform
from .errors import ensure_identifier

_PLATFORM = Platform.TIDAL


def text_value(value: Any, *attributes: str) -> str:
    """Read the first meaningful provider string attribute."""

    for attribute in attributes:
        current = getattr(value, attribute, None)
        if current is not None and str(current):
            return str(current)
    return ""


def number_value(value: Any, attribute: str) -> int | None:
    """Read an optional integer provider attribute safely."""

    current = getattr(value, attribute, None)
    return int(current) if isinstance(current, (int, float)) else None


def date_value(value: Any, *attributes: str) -> str | None:
    """Serialize the first available provider date without locale conversion."""

    for attribute in attributes:
        current = getattr(value, attribute, None)
        if hasattr(current, "isoformat"):
            return current.isoformat()
        if isinstance(current, str) and current:
            return current
    return None


def optional_string(value: Any) -> str | None:
    """Convert a non-empty value to a string, or return ``None``."""

    return str(value) if value is not None and str(value) else None


def artist_names(value: Any) -> list[str]:
    """Extract artist names from either compact or full provider objects."""

    artist = getattr(value, "artist", None)
    name = text_value(artist, "name")
    if name:
        return [name]
    artists = getattr(value, "artists", None)
    if isinstance(artists, list) and artists:
        names = [text_value(item, "name") for item in artists]
        return [item for item in names if item]
    return []


def is_video(value: Any) -> bool:
    """Return whether a provider object represents a video.

    Class-name inspection is used because ``tidalapi`` exposes videos through
    several classes and no stable public flag.
    """

    return "video" in type(value).__name__.lower()


def artist_from_tidal(value: Any) -> Artist:
    """Map a TIDAL artist object onto the universal :class:`Artist`."""

    return Artist(
        source_platform=_PLATFORM,
        source_id=ensure_identifier(value),
        name=text_value(value, "name"),
    )


def artists_from_tidal(value: Any) -> tuple[Artist, ...]:
    """Map the artist credits of a track or album."""

    artists = getattr(value, "artists", None)
    if isinstance(artists, list) and artists:
        mapped: list[Artist] = []
        for item in artists:
            name = text_value(item, "name")
            identifier = getattr(item, "id", None)
            if not name and identifier is None:
                continue
            mapped.append(
                Artist(
                    source_platform=_PLATFORM,
                    source_id=str(identifier) if identifier is not None else f"name:{name}",
                    name=name,
                )
            )
        if mapped:
            return tuple(mapped)
    credits = artist_names(value)
    return tuple(
        Artist(source_platform=_PLATFORM, source_id=f"name:{name}", name=name)
        for name in credits
    )


def album_from_tidal(value: Any) -> Album:
    """Map a TIDAL album object onto the universal :class:`Album`."""

    return Album(
        source_platform=_PLATFORM,
        source_id=ensure_identifier(value),
        title=text_value(value, "name", "title"),
        artists=artists_from_tidal(value),
        release_date=date_value(value, "available_release_date", "release_date"),
        upc=optional_string(getattr(value, "upc", None)),
        metadata={"duration_seconds": number_value(value, "duration")}
        if number_value(value, "duration") is not None
        else {},
    )


def track_from_tidal(value: Any, *, date_added: str | None = None) -> Track:
    """Map a TIDAL track object onto the universal :class:`Track`.

    TIDAL reports duration in **seconds**; the universal model stores
    milliseconds, so the conversion happens here once.
    """

    album_value = getattr(value, "album", None)
    duration_seconds = number_value(value, "duration")
    return Track(
        source_platform=_PLATFORM,
        source_id=ensure_identifier(value),
        title=text_value(value, "name", "title"),
        artists=artists_from_tidal(value),
        album=album_from_tidal(album_value) if album_value is not None else None,
        isrc=optional_string(getattr(value, "isrc", None)),
        duration_ms=duration_seconds * 1000 if duration_seconds is not None else None,
        explicit=bool(getattr(value, "explicit", False))
        if getattr(value, "explicit", None) is not None
        else None,
        version=optional_string(getattr(value, "version", None)),
        release_date=date_value(value, "release_date", "available_release_date"),
        date_added=date_added or date_value(value, "user_date_added"),
        metadata={
            "audio_quality": optional_string(getattr(value, "audio_quality", None)),
            "available": bool(getattr(value, "available", True)),
        },
    )


def playlist_item_from_tidal(value: Any, position: int) -> PlaylistItem:
    """Map one TIDAL playlist entry onto a universal :class:`PlaylistItem`.

    Position and per-occurrence identity are preserved so that duplicate
    entries survive a transfer (Invariant D). Real videos map directly to
    typed :class:`LibraryRecord` payloads instead of being converted into
    Track domain objects.
    """

    source_id = ensure_identifier(value)
    date_added = date_value(value, "date_added")

    if is_video(value):
        duration_seconds = number_value(value, "duration")
        video_meta: dict[str, Any] = {}
        if duration_seconds is not None:
            video_meta["duration_seconds"] = duration_seconds
        release_date = date_value(value, "release_date", "available_release_date")
        if release_date:
            video_meta["release_date"] = release_date

        return PlaylistItem(
            position=position,
            track=None,
            video=LibraryRecord(
                source_platform=_PLATFORM,
                source_id=source_id,
                title=text_value(value, "name", "title"),
                metadata=video_meta,
            ),
            source_item_id=source_id,
            date_added=date_added,
            metadata={"kind": "video"},
        )

    return PlaylistItem(
        position=position,
        track=track_from_tidal(value),
        video=None,
        source_item_id=source_id,
        date_added=date_added,
        metadata={"kind": "track"},
    )


def playlist_from_tidal(
    value: Any,
    *,
    items: list[PlaylistItem] | None = None,
    account_id: str = "",
    folder_id: str | None = None,
) -> Playlist:
    """Map a TIDAL playlist object onto the universal :class:`Playlist`."""

    creator = getattr(value, "creator", None)
    creator_id = str(getattr(creator, "id", "") or "")
    owned = "userplaylist" in type(value).__name__.lower() or (
        bool(creator_id) and creator_id == account_id
    )
    norm_folder_id = None if folder_id in (None, "root", "") else folder_id
    return Playlist(
        source_platform=_PLATFORM,
        source_id=ensure_identifier(value),
        name=text_value(value, "name", "title"),
        description=text_value(value, "description") or None,
        is_public=bool(getattr(value, "public", False))
        if getattr(value, "public", None) is not None
        else None,
        is_owned=owned,
        owner_id=creator_id or None,
        folder_id=norm_folder_id,
        date_added=date_value(value, "created"),
        tracks=list(items or []),
        metadata={
            "duration_seconds": number_value(value, "duration"),
            "number_of_tracks": number_value(value, "num_tracks"),
        },
    )


def folder_record_from_tidal(value: Any, parent_id: str | None = None) -> LibraryRecord:
    """Map a TIDAL playlist folder onto a generic library record."""

    norm_parent = None if parent_id in (None, "root", "") else parent_id
    return LibraryRecord(
        source_platform=_PLATFORM,
        source_id=ensure_identifier(value),
        title=text_value(value, "name"),
        metadata={
            "parent_source_id": norm_parent,
            "parent_id": norm_parent,
            "created_at": date_value(value, "created") or "",
        },
    )


def record_from_tidal(value: Any) -> LibraryRecord:
    """Map a TIDAL video or mix onto a generic library record."""

    return LibraryRecord(
        source_platform=_PLATFORM,
        source_id=ensure_identifier(value),
        title=text_value(value, "name", "title"),
    )

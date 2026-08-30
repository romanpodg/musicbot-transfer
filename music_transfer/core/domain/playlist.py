"""Playlist models where sequence and duplicates are first-class.

A playlist is deliberately **not** modelled as ``list[Track]``.  Real playlists
require:

* explicit positions that survive reordering;
* duplicate occurrences of the same track (Invariant D);
* per-item source identifiers distinct from the track identifier;
* per-item "date added";
* placeholders for items the source can no longer resolve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..enums import Platform
from .track import Track


@dataclass(slots=True)
class PlaylistItem:
    """One entry in a playlist, preserving position and identity.

    Attributes:
        position: Zero-based index in the source playlist.
        track: The resolved track, or ``None`` when the source could not
            provide it (a removed or region-blocked entry).
        source_item_id: The platform's identifier for *this occurrence*, when
            it differs from the track id.  Playlist entries on some platforms
            have their own item id, which is what makes duplicates addressable.
        date_added: ISO-8601 timestamp of when the item entered the playlist.
        metadata: Platform-specific extras.
    """

    position: int
    track: Track | None = None
    source_item_id: str | None = None
    date_added: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def track_id(self) -> str | None:
        """Return the underlying track id, or ``None`` if unresolved."""

        return self.track.source_id if self.track is not None else None

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "position": self.position,
            "track": self.track.as_dict() if self.track is not None else None,
            "source_item_id": self.source_item_id,
            "date_added": self.date_added,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PlaylistItem:
        """Rebuild a playlist item from persisted data."""

        track_value = value.get("track")
        return cls(
            position=int(value.get("position", 0)),
            track=Track.from_dict(track_value) if isinstance(track_value, dict) else None,
            source_item_id=value.get("source_item_id"),
            date_added=value.get("date_added"),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(slots=True)
class Playlist:
    """A playlist as seen on one platform.

    ``tracks`` is a mutable list on purpose: exporters append items as pages
    arrive.  Order is the source order; duplicates are preserved verbatim.
    """

    source_platform: Platform
    source_id: str
    name: str
    description: str | None = None
    is_public: bool | None = None
    is_owned: bool | None = None
    image_url: str | None = None
    owner_id: str | None = None
    folder_id: str | None = None
    date_added: str | None = None
    tracks: list[PlaylistItem] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.source_id):
            raise ValueError("playlist_source_id_missing")

    @property
    def track_count(self) -> int:
        """Return the number of entries, including duplicates."""

        return len(self.tracks)

    def ordered_track_ids(self) -> list[str]:
        """Return the track ids in playlist order, skipping unresolved items.

        Duplicate occurrences appear more than once; this is required for order
        verification and for resuming an interrupted playlist write.
        """

        return [item.track_id for item in self.tracks if item.track_id is not None]

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "source_platform": str(self.source_platform),
            "source_id": self.source_id,
            "name": self.name,
            "description": self.description,
            "is_public": self.is_public,
            "is_owned": self.is_owned,
            "image_url": self.image_url,
            "owner_id": self.owner_id,
            "folder_id": self.folder_id,
            "date_added": self.date_added,
            "tracks": [item.as_dict() for item in self.tracks],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Playlist:
        """Rebuild a playlist from persisted data."""

        return cls(
            source_platform=Platform(str(value.get("source_platform"))),
            source_id=str(value.get("source_id", "")),
            name=str(value.get("name", "")),
            description=value.get("description"),
            is_public=value.get("is_public"),
            is_owned=value.get("is_owned"),
            image_url=value.get("image_url"),
            owner_id=value.get("owner_id"),
            folder_id=value.get("folder_id"),
            date_added=value.get("date_added"),
            tracks=[
                PlaylistItem.from_dict(item) for item in (value.get("tracks") or [])
            ],
            metadata=dict(value.get("metadata") or {}),
        )

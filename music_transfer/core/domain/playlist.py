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
from typing import TYPE_CHECKING, Any

from ..enums import EntityType, Platform
from .track import Track

if TYPE_CHECKING:
    from .library import LibraryRecord


@dataclass(frozen=True, slots=True)
class PlaylistMediaRef:
    """A typed destination media reference for playlist sequence operations.

    Distinguishes tracks from videos in heterogeneous playlists even when
    underlying string identifiers overlap.
    """

    entity_type: EntityType
    media_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.entity_type, EntityType):
            try:
                object.__setattr__(self, "entity_type", EntityType(str(self.entity_type)))
            except ValueError as err:
                raise ValueError(f"invalid_entity_type:{self.entity_type}") from err
        if self.entity_type not in (EntityType.TRACK, EntityType.VIDEO):
            raise ValueError(f"unsupported_playlist_media_type:{self.entity_type}")
        if not self.media_id or not str(self.media_id).strip():
            raise ValueError("playlist_media_id_missing")

    def canonical_token(self) -> str:
        """Return a string token distinguishing entity type and media id."""
        return f"{self.entity_type.value}:{self.media_id}"

    def as_dict(self) -> dict[str, str]:
        """Serialize to JSON-compatible values."""
        return {
            "entity_type": self.entity_type.value,
            "media_id": self.media_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaylistMediaRef:
        """Rebuild from persisted data."""
        return cls(
            entity_type=EntityType(str(data["entity_type"])),
            media_id=str(data["media_id"]),
        )


@dataclass(slots=True)
class PlaylistItem:
    """One entry in a playlist, preserving position and identity.

    Attributes:
        position: Zero-based index in the source playlist.
        track: The resolved track payload, or ``None`` when the entry is a video
            or unresolved placeholder.
        video: The resolved video payload (:class:`LibraryRecord`), or ``None``
            when the entry is a track or unresolved placeholder.
        source_item_id: The platform's identifier for *this occurrence*, when
            it differs from the media id.  Playlist entries on some platforms
            have their own item id, which is what makes duplicates addressable.
        date_added: ISO-8601 timestamp of when the item entered the playlist.
        metadata: Platform-specific extras.
    """

    position: int
    track: Track | None = None
    video: LibraryRecord | None = None
    source_item_id: str | None = None
    date_added: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.track is not None and self.video is not None:
            raise ValueError("playlist_item_multiple_payloads")

    @property
    def media_entity_type(self) -> EntityType | None:
        """Return the underlying media EntityType, or ``None`` if unresolved."""
        if self.track is not None:
            return EntityType.TRACK
        if self.video is not None:
            return EntityType.VIDEO
        return None

    @property
    def media_id(self) -> str | None:
        """Return the underlying media identifier, or ``None`` if unresolved."""
        if self.track is not None:
            return self.track.source_id
        if self.video is not None:
            return self.video.source_id
        return None

    @property
    def track_id(self) -> str | None:
        """Return the underlying track id, or ``None`` if unresolved."""
        return self.track.source_id if self.track is not None else None

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""
        return {
            "position": self.position,
            "track": self.track.as_dict() if self.track is not None else None,
            "video": self.video.as_dict() if self.video is not None else None,
            "source_item_id": self.source_item_id,
            "date_added": self.date_added,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PlaylistItem:
        """Rebuild a playlist item from persisted data with legacy compatibility."""
        from .library import LibraryRecord

        position = int(value.get("position", 0))
        source_item_id = value.get("source_item_id")
        date_added = value.get("date_added")
        metadata = dict(value.get("metadata") or {})

        track_value = value.get("track")
        video_value = value.get("video")

        track = Track.from_dict(track_value) if isinstance(track_value, dict) else None
        video = LibraryRecord.from_dict(video_value) if isinstance(video_value, dict) else None

        # Section 11: Legacy video snapshot compatibility
        # If metadata.kind == "video" AND video is absent AND legacy track payload exists:
        if video is None and metadata.get("kind") == "video" and track is not None:
            video = LibraryRecord(
                source_platform=track.source_platform,
                source_id=track.source_id,
                title=track.title,
                metadata=dict(track.metadata),
            )
            track = None

        return cls(
            position=position,
            track=track,
            video=video,
            source_item_id=source_item_id,
            date_added=date_added,
            metadata=metadata,
        )


@dataclass(slots=True)
class Playlist:
    """A playlist as seen on one platform.

    ``tracks`` is a mutable list of :class:`PlaylistItem` entries (which may
    represent TRACK or VIDEO items). Order is the source order; duplicates are
    preserved verbatim.
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

    def ordered_media_refs(self) -> list[PlaylistMediaRef]:
        """Return the typed media refs in playlist order, skipping unresolved items."""
        refs: list[PlaylistMediaRef] = []
        for item in self.tracks:
            m_type = item.media_entity_type
            m_id = item.media_id
            if m_type is not None and m_id is not None:
                refs.append(PlaylistMediaRef(entity_type=m_type, media_id=m_id))
        return refs

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

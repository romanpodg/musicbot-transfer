"""The platform-independent track model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..enums import Platform
from .album import Album
from .artist import Artist, artist_names


@dataclass(frozen=True, slots=True)
class Track:
    """A track as seen on one platform.

    Only fields that the transfer core compares or reports get an explicit
    attribute.  Everything else lives in ``metadata`` so that adding a platform
    never forces a change here.

    ``version`` holds a normalized version qualifier such as ``Remastered`` or
    ``Live`` when the source platform exposes one.  It is informational; the
    matcher re-derives qualifiers from the title as well, because platforms are
    inconsistent about separating them.
    """

    source_platform: Platform
    source_id: str
    title: str
    artists: tuple[Artist, ...] = ()
    album: Album | None = None
    isrc: str | None = None
    duration_ms: int | None = None
    explicit: bool | None = None
    version: str | None = None
    release_date: str | None = None
    date_added: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.source_id):
            raise ValueError("track_source_id_missing")

    @property
    def primary_artist(self) -> Artist | None:
        """Return the first credited artist, if the platform provided one."""

        return self.artists[0] if self.artists else None

    @property
    def artist_names(self) -> list[str]:
        """Return every credited artist name, in credited order."""

        return artist_names(self.artists)

    @property
    def album_title(self) -> str | None:
        """Return the album title when the source exposed an album."""

        return self.album.title if self.album is not None else None

    @property
    def duration_seconds(self) -> float | None:
        """Return the duration in seconds, or ``None`` when unknown."""

        return None if self.duration_ms is None else self.duration_ms / 1000.0

    def label(self) -> str:
        """Return a display-safe ``Artist - Title`` label for logs and UIs."""

        artist = self.artist_names[0] if self.artist_names else ""
        return f"{artist} - {self.title}" if artist else self.title

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values (never contains credentials)."""

        return {
            "source_platform": str(self.source_platform),
            "source_id": self.source_id,
            "title": self.title,
            "artists": [artist.as_dict() for artist in self.artists],
            "album": self.album.as_dict() if self.album is not None else None,
            "isrc": self.isrc,
            "duration_ms": self.duration_ms,
            "explicit": self.explicit,
            "version": self.version,
            "release_date": self.release_date,
            "date_added": self.date_added,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Track:
        """Rebuild a track from persisted data."""

        album_value = value.get("album")
        return cls(
            source_platform=Platform(str(value.get("source_platform"))),
            source_id=str(value.get("source_id", "")),
            title=str(value.get("title", "")),
            artists=tuple(
                Artist.from_dict(item) for item in (value.get("artists") or [])
            ),
            album=Album.from_dict(album_value) if isinstance(album_value, dict) else None,
            isrc=value.get("isrc"),
            duration_ms=value.get("duration_ms"),
            explicit=value.get("explicit"),
            version=value.get("version"),
            release_date=value.get("release_date"),
            date_added=value.get("date_added"),
            metadata=dict(value.get("metadata") or {}),
        )

"""The platform-independent album model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..enums import Platform
from .artist import Artist, artist_names


@dataclass(frozen=True, slots=True)
class Album:
    """An album as seen on one platform.

    ``upc`` is optional because several platforms do not expose it.  A missing
    UPC must never break a transfer; it only reduces matching confidence.
    """

    source_platform: Platform
    source_id: str
    title: str
    artists: tuple[Artist, ...] = ()
    release_date: str | None = None
    upc: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.source_id):
            raise ValueError("album_source_id_missing")

    @property
    def artist_names(self) -> list[str]:
        """Return the album's artist names in order."""

        return artist_names(self.artists)

    @property
    def primary_artist(self) -> Artist | None:
        """Return the first credited artist, if any."""

        return self.artists[0] if self.artists else None

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "source_platform": str(self.source_platform),
            "source_id": self.source_id,
            "title": self.title,
            "artists": [artist.as_dict() for artist in self.artists],
            "release_date": self.release_date,
            "upc": self.upc,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Album:
        """Rebuild an album from persisted data."""

        return cls(
            source_platform=Platform(str(value.get("source_platform"))),
            source_id=str(value.get("source_id", "")),
            title=str(value.get("title", "")),
            artists=tuple(
                Artist.from_dict(item) for item in (value.get("artists") or [])
            ),
            release_date=value.get("release_date"),
            upc=value.get("upc"),
            metadata=dict(value.get("metadata") or {}),
        )

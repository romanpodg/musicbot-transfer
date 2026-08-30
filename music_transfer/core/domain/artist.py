"""The platform-independent artist model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..enums import Platform


@dataclass(frozen=True, slots=True)
class Artist:
    """An artist as seen on one platform.

    ``metadata`` carries platform-specific extras (TIDAL picture ids, Spotify
    genres, ...).  Only values that the transfer core genuinely compares get an
    explicit field, which keeps the core free of ``dict[str, Any]`` lookups.
    """

    source_platform: Platform
    source_id: str
    name: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.source_id):
            raise ValueError("artist_source_id_missing")

    @property
    def display_name(self) -> str:
        """Return a human-readable name, falling back to the identifier."""

        return self.name or str(self.source_id)

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values (no credentials, ever)."""

        return {
            "source_platform": str(self.source_platform),
            "source_id": self.source_id,
            "name": self.name,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Artist:
        """Rebuild an artist from persisted data."""

        return cls(
            source_platform=Platform(str(value.get("source_platform"))),
            source_id=str(value.get("source_id", "")),
            name=str(value.get("name", "")),
            metadata=dict(value.get("metadata") or {}),
        )


def artist_names(artists: tuple[Artist, ...] | list[Artist]) -> list[str]:
    """Return the artist names in order, skipping nameless placeholders."""

    return [artist.name for artist in artists if artist.name]

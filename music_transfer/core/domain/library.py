"""A platform-independent library snapshot.

A snapshot is the read-only result of exporting one account.  Backups, cleanup
plans, and verification all operate on snapshots rather than on live platform
clients, which keeps destructive planning auditable.

``videos``, ``mixes`` and ``folders`` use :class:`LibraryRecord` rather than a
dedicated model: they are currently single-platform concepts (TIDAL) and giving
them a full universal model before a second platform needs one would be
speculative.  They stay typed instead of raw dictionaries so the core never has
to inspect untagged data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..enums import Platform
from .account import AccountProfile
from .album import Album
from .artist import Artist
from .playlist import Playlist
from .track import Track
from .transfer import utc_now


@dataclass(frozen=True, slots=True)
class LibraryRecord:
    """A typed stand-in for a library object without a full universal model."""

    source_platform: Platform
    source_id: str
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "source_platform": str(self.source_platform),
            "source_id": self.source_id,
            "title": self.title,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LibraryRecord:
        """Rebuild a record from persisted data."""

        return cls(
            source_platform=Platform(str(value.get("source_platform"))),
            source_id=str(value.get("source_id", "")),
            title=str(value.get("title", "")),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(slots=True)
class LibrarySnapshot:
    """A credential-free snapshot of one library.

    ``incomplete_sections`` is the honest record of a partial export.  A section
    the platform could not read is listed here instead of being silently
    omitted, so a cleanup plan can refuse to act on an incomplete view.
    """

    account: AccountProfile
    platform: Platform
    captured_at: str = field(default_factory=utc_now)
    tracks: list[Track] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    artists: list[Artist] = field(default_factory=list)
    playlists: list[Playlist] = field(default_factory=list)
    videos: list[LibraryRecord] = field(default_factory=list)
    mixes: list[LibraryRecord] = field(default_factory=list)
    folders: list[LibraryRecord] = field(default_factory=list)
    incomplete_sections: list[str] = field(default_factory=list)

    #: Section names that always exist, in stable reporting order.
    SECTIONS: tuple[str, ...] = (
        "tracks",
        "albums",
        "artists",
        "videos",
        "mixes",
        "folders",
        "playlists",
    )

    def counts(self) -> dict[str, int]:
        """Return per-section counts using stable names for UI and reports."""

        return {section: len(getattr(self, section)) for section in self.SECTIONS}

    @property
    def is_partial(self) -> bool:
        """Return whether any section could not be fully exported."""

        return bool(self.incomplete_sections)

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values.  Never contains credentials."""

        return {
            "account": self.account.as_dict(),
            "platform": str(self.platform),
            "captured_at": self.captured_at,
            "tracks": [track.as_dict() for track in self.tracks],
            "albums": [album.as_dict() for album in self.albums],
            "artists": [artist.as_dict() for artist in self.artists],
            "playlists": [playlist.as_dict() for playlist in self.playlists],
            "videos": [record.as_dict() for record in self.videos],
            "mixes": [record.as_dict() for record in self.mixes],
            "folders": [record.as_dict() for record in self.folders],
            "incomplete_sections": list(self.incomplete_sections),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LibrarySnapshot:
        """Rebuild a snapshot, validating the minimum required shape."""

        account_value = value.get("account")
        if not isinstance(account_value, dict):
            raise ValueError("snapshot_account_missing")
        return cls(
            account=AccountProfile.from_dict(account_value),
            platform=Platform(str(value.get("platform"))),
            captured_at=str(value.get("captured_at", utc_now())),
            tracks=[Track.from_dict(item) for item in _list_of(value, "tracks")],
            albums=[Album.from_dict(item) for item in _list_of(value, "albums")],
            artists=[Artist.from_dict(item) for item in _list_of(value, "artists")],
            playlists=[Playlist.from_dict(item) for item in _list_of(value, "playlists")],
            videos=[LibraryRecord.from_dict(item) for item in _list_of(value, "videos")],
            mixes=[LibraryRecord.from_dict(item) for item in _list_of(value, "mixes")],
            folders=[LibraryRecord.from_dict(item) for item in _list_of(value, "folders")],
            incomplete_sections=[
                str(section) for section in (value.get("incomplete_sections") or [])
            ],
        )


def _list_of(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return the dict-valued entries stored under ``key``.

    Unknown or malformed sections degrade to empty rather than raising, because
    a snapshot written by an older release may legitimately lack a section.
    """

    section = value.get(key)
    if not isinstance(section, list):
        return []
    return [item for item in section if isinstance(item, dict)]

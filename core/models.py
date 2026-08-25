"""Provider-neutral library snapshots and operation reports.

These models deliberately contain library metadata only.  OAuth credentials
never enter a backup, report, or resumable transfer state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    """Return an ISO-8601 timestamp in UTC."""

    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class AccountProfile:
    """A safe, display-oriented representation of an authenticated account."""

    account_id: str
    display_name: str | None

    def as_dict(self) -> dict[str, str | None]:
        """Serialize the profile without any authentication material."""

        return {"account_id": self.account_id, "display_name": self.display_name}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AccountProfile:
        """Create a profile from persisted non-secret metadata."""

        return cls(
            account_id=str(value.get("account_id", "")),
            display_name=value.get("display_name"),
        )


@dataclass(slots=True)
class LibrarySnapshot:
    """A portable, credential-free snapshot of a TIDAL library."""

    account: AccountProfile
    captured_at: str = field(default_factory=utc_now)
    tracks: list[dict[str, Any]] = field(default_factory=list)
    albums: list[dict[str, Any]] = field(default_factory=list)
    artists: list[dict[str, Any]] = field(default_factory=list)
    videos: list[dict[str, Any]] = field(default_factory=list)
    mixes: list[dict[str, Any]] = field(default_factory=list)
    folders: list[dict[str, Any]] = field(default_factory=list)
    playlists: list[dict[str, Any]] = field(default_factory=list)
    incomplete_sections: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """Return stable count names used by the UI and verification report."""

        return {
            "tracks": len(self.tracks),
            "albums": len(self.albums),
            "artists": len(self.artists),
            "videos": len(self.videos),
            "mixes": len(self.mixes),
            "folders": len(self.folders),
            "playlists": len(self.playlists),
        }

    def as_dict(self) -> dict[str, Any]:
        """Serialize the snapshot into JSON-compatible values."""

        return {
            "account": self.account.as_dict(),
            "captured_at": self.captured_at,
            "tracks": self.tracks,
            "albums": self.albums,
            "artists": self.artists,
            "videos": self.videos,
            "mixes": self.mixes,
            "folders": self.folders,
            "playlists": self.playlists,
            "incomplete_sections": self.incomplete_sections,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LibrarySnapshot:
        """Validate the minimum backup shape and construct a snapshot."""

        account = value.get("account")
        if not isinstance(account, dict):
            raise ValueError("backup_account_missing")
        return cls(
            account=AccountProfile.from_dict(account),
            captured_at=str(value.get("captured_at", utc_now())),
            tracks=_dict_list(value.get("tracks")),
            albums=_dict_list(value.get("albums")),
            artists=_dict_list(value.get("artists")),
            videos=_dict_list(value.get("videos")),
            mixes=_dict_list(value.get("mixes")),
            folders=_dict_list(value.get("folders")),
            playlists=_dict_list(value.get("playlists")),
            incomplete_sections=[
                str(section) for section in value.get("incomplete_sections", [])
            ],
        )


def _dict_list(value: Any) -> list[dict[str, Any]]:
    """Return a defensive copy of a list of JSON object values."""

    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("backup_section_invalid")
    return [dict(item) for item in value]


@dataclass(slots=True)
class TransferReport:
    """Collect sanitized per-item results for a transfer or restore."""

    operation: str
    started_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    source_counts: dict[str, int] = field(default_factory=dict)
    destination_counts: dict[str, int] = field(default_factory=dict)
    successful_items: list[dict[str, str]] = field(default_factory=list)
    failed_items: list[dict[str, str]] = field(default_factory=list)
    unavailable_items: list[dict[str, str]] = field(default_factory=list)
    skipped_items: list[dict[str, str]] = field(default_factory=list)

    def add(self, status: str, category: str, item_id: str, reason: str = "") -> None:
        """Append a sanitized operation outcome to its report bucket."""

        event = {"category": category, "id": item_id}
        if reason:
            event["reason"] = reason
        buckets = {
            "successful": self.successful_items,
            "failed": self.failed_items,
            "unavailable": self.unavailable_items,
            "skipped": self.skipped_items,
        }
        buckets[status].append(event)

    def finish(self) -> None:
        """Mark the report complete."""

        self.completed_at = utc_now()

    def has_retryable_failures(self) -> bool:
        """Whether state should remain available for a future retry."""

        return bool(self.failed_items or self.unavailable_items)

    def as_dict(self) -> dict[str, Any]:
        """Serialize the report for JSON output."""

        return {
            "format_version": 1,
            "operation": self.operation,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "source_counts": self.source_counts,
            "destination_counts": self.destination_counts,
            "successful_items": self.successful_items,
            "failed_items": self.failed_items,
            "unavailable_items": self.unavailable_items,
            "skipped_items": self.skipped_items,
        }

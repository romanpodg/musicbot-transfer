"""Portable, versioned TIDAL library backups."""

from __future__ import annotations

import logging
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .auth import ProgressCallback, TidalLibraryClient
from .models import LibrarySnapshot, utc_now
from .state import atomic_write_json, read_json


BACKUP_FORMAT = "tidal-library-manager-backup"
BACKUP_VERSION = 2


class BackupFormatError(ValueError):
    """Raised when a selected file is not a supported manager backup."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    """The durable result of a backup operation."""

    path: Path
    snapshot: LibrarySnapshot

    @property
    def is_partial(self) -> bool:
        """Whether one or more remote library sections could not be exported."""

        return bool(self.snapshot.incomplete_sections)


class BackupService:
    """Create and read credential-free JSON library backups."""

    def __init__(self, default_path: Path, logger: logging.Logger | None = None) -> None:
        self.default_path = default_path
        self._logger = logger or logging.getLogger("tidal_manager")

    def create(
        self,
        client: TidalLibraryClient,
        progress: ProgressCallback | None = None,
        path: Path | None = None,
    ) -> BackupResult:
        """Export a full supported library snapshot and save it atomically."""

        self._logger.info("event=backup_started")
        snapshot = client.export_library(progress)
        target = path or self.default_path
        library = snapshot.as_dict()
        checksum = _checksum(library)
        atomic_write_json(
            target,
            {
                "format": BACKUP_FORMAT,
                "format_version": BACKUP_VERSION,
                "created_at": utc_now(),
                "status": "complete" if not snapshot.incomplete_sections else "incomplete",
                "source_account_id": snapshot.account.account_id,
                "counts": snapshot.counts(),
                "checksum_sha256": checksum,
                "library": library,
            },
        )
        self._logger.info(
            "event=backup_completed path=%s partial=%s", target.name, bool(snapshot.incomplete_sections)
        )
        return BackupResult(path=target, snapshot=snapshot)

    def load(self, path: Path) -> LibrarySnapshot:
        """Load and validate a versioned backup without executing remote calls."""

        try:
            value = read_json(path)
            if value.get("format") != BACKUP_FORMAT:
                raise BackupFormatError("backup_format_invalid")
            version = value.get("format_version")
            if version not in {1, BACKUP_VERSION}:
                raise BackupFormatError("backup_version_unsupported")
            library = value.get("library")
            if not isinstance(library, dict):
                raise BackupFormatError("backup_library_missing")
            snapshot = LibrarySnapshot.from_dict(library)
            if version == BACKUP_VERSION:
                if value.get("status") not in {"complete", "incomplete"}:
                    raise BackupFormatError("backup_completion_invalid")
                if value.get("checksum_sha256") != _checksum(library):
                    raise BackupFormatError("backup_checksum_invalid")
                if value.get("source_account_id") != snapshot.account.account_id:
                    raise BackupFormatError("backup_account_mismatch")
            self._logger.info("event=backup_loaded path=%s", path.name)
            return snapshot
        except (OSError, ValueError, TypeError) as error:
            if isinstance(error, BackupFormatError):
                raise
            self._logger.error("event=backup_load_failed error_type=%s", type(error).__name__)
            raise BackupFormatError("backup_read_failed") from None

    def list_backups(self) -> list[Path]:
        """Return available JSON backups, newest first."""

        if not self.default_path.parent.exists():
            return []
        return sorted(
            self.default_path.parent.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )


def _checksum(library: dict[str, object]) -> str:
    """Return a deterministic integrity digest without credentials."""

    encoded = json.dumps(library, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

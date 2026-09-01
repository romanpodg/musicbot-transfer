"""Atomic JSON helpers and the JSON repository implementations.

These back the repository ports for the local CLI.  A future PostgreSQL
implementation can replace them without touching the transfer engine, because
the engine only ever sees the ports.

Durability rules:

* writes go to a temporary file, are flushed and fsynced, then atomically
  replaced, so an interrupted write cannot leave a truncated state file;
* files are created with owner-only permissions where the OS supports it;
* reads validate the top-level shape and raise :class:`PersistenceError` rather
  than returning half-parsed data.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ...core.errors import PersistenceError

#: The on-disk format version, bumped whenever the layout changes incompatibly.
STATE_FORMAT_VERSION = 2


def ensure_directories(root: Path, *names: str) -> None:
    """Create the application data directories when they are absent."""

    for name in names:
        (root / name).mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a JSON object and restrict the file where possible."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent, text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        with contextlib.suppress(OSError):
            # A platform without POSIX permissions must not break persistence.
            os.chmod(path, 0o600)
    except OSError as error:
        raise PersistenceError("atomic_write_failed") from error
    finally:
        if temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object, rejecting invalid or unexpected content."""

    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as error:
        raise PersistenceError("state_missing") from error
    except (OSError, json.JSONDecodeError) as error:
        raise PersistenceError("state_unreadable") from error
    if not isinstance(value, dict):
        raise PersistenceError("json_object_expected")
    return value


class JsonDocumentStore:
    """A single JSON document that is replaced atomically on every save."""

    def __init__(self, path: Path, *, format_version: int = STATE_FORMAT_VERSION) -> None:
        self.path = path
        self.format_version = format_version

    def exists(self) -> bool:
        """Return whether the document exists."""

        return self.path.is_file()

    def read(self) -> dict[str, Any]:
        """Read and validate the document."""

        return read_json(self.path)

    def write(self, payload: dict[str, Any]) -> None:
        """Atomically replace the document."""

        atomic_write_json(self.path, {**payload, "format_version": self.format_version})

    def clear(self) -> None:
        """Remove the document if it exists."""

        self.path.unlink(missing_ok=True)

"""Configuration, durable transfer checkpoints, and safe application logging."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import LibrarySnapshot, utc_now


DATA_DIRECTORIES = ("backups", "reports", "logs", "state")


def ensure_data_directories(project_root: Path) -> None:
    """Create the application data directory tree when it is absent."""

    for directory_name in DATA_DIRECTORIES:
        (project_root / "data" / directory_name).mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON and restrict the file where the OS supports it."""

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
        _sync_parent_directory(path.parent)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object, rejecting invalid or unexpected content."""

    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("json_object_expected")
    return value


def _sync_parent_directory(path: Path) -> None:
    """Best-effort POSIX rename durability; Windows does not support this."""

    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Replacement itself succeeded; directory fsync is an optional
        # durability upgrade and must not break Windows or network shares.
        return


@dataclass(slots=True)
class ApplicationConfig:
    """Non-secret, user-editable configuration."""

    language: str | None = None
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: Path) -> ApplicationConfig:
        """Load configuration or create a safe default configuration."""

        if not path.exists():
            config = cls()
            atomic_write_json(path, asdict(config))
            return config
        value = read_json(path)
        language = value.get("language")
        return cls(
            language=language if isinstance(language, str) else None,
            log_level=str(value.get("log_level", "INFO")).upper(),
        )

    def save(self, path: Path) -> None:
        """Persist only non-secret configuration."""

        atomic_write_json(path, asdict(self))


@dataclass(slots=True)
class TransferState:
    """Credential-free checkpoint data required for an exact resume."""

    operation: str
    source_snapshot: LibrarySnapshot
    sort_order: str = "original"
    created_at: str = field(default_factory=utc_now)
    current_category: str | None = None
    current_playlist: str | None = None
    current_position: int = 0
    completed_objects: dict[str, list[str]] = field(default_factory=dict)
    failed_items: dict[str, list[str]] = field(default_factory=dict)
    item_statuses: dict[str, str] = field(default_factory=dict)
    retry_counts: dict[str, int] = field(default_factory=dict)
    ambiguous_objects: dict[str, list[str]] = field(default_factory=dict)
    destination_playlists: dict[str, str] = field(default_factory=dict)
    destination_folders: dict[str, str] = field(default_factory=dict)
    last_applied_seq: int = 0

    @classmethod
    def create(
        cls, operation: str, source_snapshot: LibrarySnapshot, sort_order: str = "original"
    ) -> TransferState:
        """Create a new checkpoint before the first remote mutation."""

        return cls(
            operation=operation,
            source_snapshot=source_snapshot,
            sort_order=sort_order,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TransferState:
        """Load a checkpoint, preserving only supported fields."""

        version = value.get("format_version", 1)
        if not isinstance(version, int) or version not in {1, 2, 3, 4}:
            raise ValueError("transfer_state_version_unsupported")

        snapshot_value = value.get("source_snapshot")
        if not isinstance(snapshot_value, dict):
            raise ValueError("transfer_snapshot_missing")
        completed = value.get("completed_objects", {})
        if not isinstance(completed, dict):
            raise ValueError("transfer_completed_invalid")
        failed = value.get("failed_items", {})
        statuses = value.get("item_statuses", {})
        retries = value.get("retry_counts", {})
        ambiguous = value.get("ambiguous_objects", {})
        return cls(
            operation=str(value.get("operation", "transfer")),
            source_snapshot=LibrarySnapshot.from_dict(snapshot_value),
            sort_order=str(value.get("sort_order", "original")),
            created_at=str(value.get("created_at", utc_now())),
            current_category=_optional_string(value.get("current_category")),
            current_playlist=_optional_string(value.get("current_playlist")),
            current_position=max(0, int(value.get("current_position", 0))),
            completed_objects={
                str(category): [str(item_id) for item_id in item_ids]
                for category, item_ids in completed.items()
                if isinstance(item_ids, list)
            },
            failed_items={
                str(category): [str(item_id) for item_id in item_ids]
                for category, item_ids in failed.items()
                if isinstance(item_ids, list)
            }
            if isinstance(failed, dict)
            else {},
            item_statuses={
                str(key): str(status)
                for key, status in statuses.items()
                if str(status) in {
                    "pending", "completed", "failed_retryable", "failed_permanent",
                    "unavailable", "unsupported", "ambiguous", "already_present",
                }
            }
            if isinstance(statuses, dict)
            else {},
            retry_counts={
                str(key): max(0, int(count))
                for key, count in retries.items()
                if isinstance(count, int)
            }
            if isinstance(retries, dict)
            else {},
            ambiguous_objects={
                str(category): [str(item_id) for item_id in item_ids]
                for category, item_ids in ambiguous.items()
                if isinstance(item_ids, list)
            }
            if isinstance(ambiguous, dict)
            else {},
            destination_playlists=_string_mapping(value.get("destination_playlists")),
            destination_folders=_string_mapping(value.get("destination_folders")),
            last_applied_seq=max(0, int(value.get("last_applied_seq", 0))),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize the checkpoint without credentials or raw exceptions."""

        return {
            "format_version": 4,
            "operation": self.operation,
            "sort_order": self.sort_order,
            "created_at": self.created_at,
            "current_category": self.current_category,
            "current_playlist": self.current_playlist,
            "current_position": self.current_position,
            "completed_objects": self.completed_objects,
            "failed_items": self.failed_items,
            "item_statuses": self.item_statuses,
            "retry_counts": self.retry_counts,
            "ambiguous_objects": self.ambiguous_objects,
            "destination_playlists": self.destination_playlists,
            "destination_folders": self.destination_folders,
            "last_applied_seq": self.last_applied_seq,
            "source_snapshot": self.source_snapshot.as_dict(),
        }

    def is_completed(self, category: str, item_id: str) -> bool:
        """Return whether a completed item is safe to skip after resumption."""

        return self.status_of(category, item_id) == "completed" or item_id in self.completed_objects.get(category, [])

    def status_of(self, category: str, item_id: str) -> str | None:
        """Return the persisted terminal/retry state for one logical object."""

        return self.item_statuses.get(f"{category}:{item_id}")

    def mark_completed(self, category: str, item_id: str) -> None:
        """Record a completed object exactly once."""

        entries = self.completed_objects.setdefault(category, [])
        if self.status_of(category, item_id) != "completed":
            entries.append(item_id)
        failed = self.failed_items.get(category, [])
        if item_id in failed:
            failed.remove(item_id)
        self.item_statuses[f"{category}:{item_id}"] = "completed"

    def mark_failed(self, category: str, item_id: str, retry_count: int) -> None:
        """Record a retryable outcome so a later run can audit and retry it."""

        entries = self.failed_items.setdefault(category, [])
        if self.status_of(category, item_id) != "failed_retryable":
            entries.append(item_id)
        self.retry_counts[f"{category}:{item_id}"] = max(0, retry_count)
        self.item_statuses[f"{category}:{item_id}"] = "failed_retryable"

    def mark_terminal(self, category: str, item_id: str, status: str) -> None:
        """Record an outcome which must not be retried automatically."""

        if status not in {"failed_permanent", "unavailable", "unsupported", "already_present"}:
            raise ValueError("invalid_terminal_status")
        entries = self.failed_items.get(category, [])
        if item_id in entries:
            entries.remove(item_id)
        self.item_statuses[f"{category}:{item_id}"] = status

    def mark_ambiguous(self, category: str, item_id: str) -> None:
        """Block automatic replay of a creation whose remote outcome is unknown."""

        entries = self.ambiguous_objects.setdefault(category, [])
        if item_id not in entries:
            entries.append(item_id)
        self.item_statuses[f"{category}:{item_id}"] = "ambiguous"

    def is_ambiguous(self, category: str, item_id: str) -> bool:
        """Return whether automatic replay would risk duplicate remote objects."""

        return item_id in self.ambiguous_objects.get(category, [])


class TransferStateStore:
    """Persist a static transfer snapshot plus small durable state events."""

    _COMPACT_AFTER = 500

    def __init__(self, path: Path) -> None:
        self.path = path
        self.journal_path = path.with_suffix(path.suffix + ".journal")
        self._known_statuses: dict[str, str] = {}
        self._known_retries: dict[str, int] = {}
        self._known_playlists: dict[str, str] = {}
        self._known_folders: dict[str, str] = {}

    def exists(self) -> bool:
        """Return whether a resumable checkpoint exists."""

        return self.path.is_file()

    def load(self) -> TransferState:
        """Load a previously persisted transfer checkpoint."""

        state = TransferState.from_dict(read_json(self.path))
        if not self.journal_path.exists():
            self._remember(state)
            return state
        with self.journal_path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1 and not line.endswith("\n"):
                    break
                raise ValueError("transfer_journal_invalid") from None
            seq = event.get("seq")
            if not isinstance(seq, int) or seq <= state.last_applied_seq:
                continue
            if seq != state.last_applied_seq + 1:
                raise ValueError("transfer_journal_sequence_invalid")
            self._apply_event(state, event)
            state.last_applied_seq = seq
        self._remember(state)
        return state

    def save(self, state: TransferState) -> None:
        """Append a small fsynced mutation event and periodically compact."""

        if not self.path.exists():
            atomic_write_json(self.path, state.as_dict())
            self._remember(state)
            return
        event = self._event_from_state(state)
        state.last_applied_seq += 1
        event["seq"] = state.last_applied_seq
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if state.last_applied_seq % self._COMPACT_AFTER == 0:
            atomic_write_json(self.path, state.as_dict())
            self.journal_path.unlink(missing_ok=True)
        self._remember(state)

    def clear(self) -> None:
        """Remove a checkpoint only after a fully successful operation."""

        self.path.unlink(missing_ok=True)
        self.journal_path.unlink(missing_ok=True)

    def _event_from_state(self, state: TransferState) -> dict[str, Any]:
        """Use per-object statuses as the canonical replayable transfer state."""

        return {
            "current_category": state.current_category,
            "current_playlist": state.current_playlist,
            "current_position": state.current_position,
            "item_statuses": {key: value for key, value in state.item_statuses.items() if self._known_statuses.get(key) != value},
            "retry_counts": {key: value for key, value in state.retry_counts.items() if self._known_retries.get(key) != value},
            "destination_playlists": {key: value for key, value in state.destination_playlists.items() if self._known_playlists.get(key) != value},
            "destination_folders": {key: value for key, value in state.destination_folders.items() if self._known_folders.get(key) != value},
        }

    def _remember(self, state: TransferState) -> None:
        self._known_statuses = dict(state.item_statuses)
        self._known_retries = dict(state.retry_counts)
        self._known_playlists = dict(state.destination_playlists)
        self._known_folders = dict(state.destination_folders)

    @staticmethod
    def _apply_event(state: TransferState, event: dict[str, Any]) -> None:
        state.current_category = _optional_string(event.get("current_category"))
        state.current_playlist = _optional_string(event.get("current_playlist"))
        state.current_position = max(0, int(event.get("current_position", 0)))
        state.item_statuses.update(_string_mapping(event.get("item_statuses")))
        if isinstance(event.get("retry_counts"), dict):
            state.retry_counts.update({str(key): max(0, int(value)) for key, value in event["retry_counts"].items()})
        state.destination_playlists.update(_string_mapping(event.get("destination_playlists")))
        state.destination_folders.update(_string_mapping(event.get("destination_folders")))
        for key, status in _string_mapping(event.get("item_statuses")).items():
            if ":" not in key:
                continue
            category, item_id = key.split(":", 1)
            if status == "completed":
                entries = state.completed_objects.setdefault(category, [])
                if item_id not in entries:
                    entries.append(item_id)
            elif status == "failed_retryable":
                entries = state.failed_items.setdefault(category, [])
                if item_id not in entries:
                    entries.append(item_id)


@dataclass(slots=True)
class DeleteState:
    """Credential-free cleanup checkpoint derived from the durable delete queue."""

    operation: str
    total: int
    completed: int = 0
    failed: int = 0
    remaining: int = 0
    current_category: str | None = None
    current_item_id: str | None = None
    interrupted: bool = False
    finished: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(cls, operation: str, total: int) -> "DeleteState":
        """Initialize a checkpoint before the first deletion request."""

        return cls(operation=operation, total=total, remaining=total)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeleteState":
        """Load a queue checkpoint while rejecting malformed numeric values."""

        version = value.get("format_version", 1)
        if not isinstance(version, int) or version != 1:
            raise ValueError("delete_state_version_unsupported")

        total = max(0, int(value.get("total", 0)))
        completed = min(total, max(0, int(value.get("completed", 0))))
        failed = min(total - completed, max(0, int(value.get("failed", 0))))
        remaining = min(total - completed - failed, max(0, int(value.get("remaining", 0))))
        return cls(
            operation=str(value.get("operation", "delete")),
            total=total,
            completed=completed,
            failed=failed,
            remaining=remaining,
            current_category=_optional_string(value.get("current_category")),
            current_item_id=_optional_string(value.get("current_item_id")),
            interrupted=bool(value.get("interrupted", False)),
            finished=bool(value.get("finished", False)),
            created_at=str(value.get("created_at", utc_now())),
            updated_at=str(value.get("updated_at", utc_now())),
        )

    def refresh(self, statuses: list[str]) -> None:
        """Synchronize aggregate counters with the authoritative queue statuses."""

        self.total = len(statuses)
        self.completed = sum(status in {"completed", "already_absent"} for status in statuses)
        self.failed = sum(status in {"failed_retryable", "failed_permanent"} for status in statuses)
        self.remaining = sum(status in {"pending", "processing", "ambiguous"} for status in statuses)
        self.updated_at = utc_now()

    def as_dict(self) -> dict[str, Any]:
        """Serialize a compact, safe cleanup resume checkpoint."""

        return {
            "format_version": 1,
            "operation": self.operation,
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "remaining": self.remaining,
            "current_category": self.current_category,
            "current_item_id": self.current_item_id,
            "interrupted": self.interrupted,
            "finished": self.finished,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DeleteStateStore:
    """Persist cleanup checkpoints atomically after every queue transition."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        """Return whether a resumable cleanup state exists."""

        return self.path.is_file()

    def load(self) -> DeleteState:
        """Read a persisted cleanup state."""

        return DeleteState.from_dict(read_json(self.path))

    def save(self, state: DeleteState) -> None:
        """Durably save the supplied cleanup state."""

        atomic_write_json(self.path, state.as_dict())

    def clear(self) -> None:
        """Remove the state only after a fully successful cleanup."""

        self.path.unlink(missing_ok=True)


class SecretRedactionFilter(logging.Filter):
    """Prevent token-shaped values from being written to the application log."""

    _patterns = (
        re.compile(r"(?i)(access[_ -]?token|refresh[_ -]?token|authorization)\s*[=:]\s*(?:bearer\s+)?[^\s,;]+"),
        re.compile(r"(?i)bearer\s+[^\s,;]+"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        """Scrub the message and discard untrusted formatting arguments."""

        message = record.getMessage()
        for pattern in self._patterns:
            message = pattern.sub("[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(path: Path, level: str = "INFO") -> logging.Logger:
    """Configure the isolated application logger with secret redaction."""

    logger = logging.getLogger("tidal_manager")
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.propagate = False
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(SecretRedactionFilter())
    logger.addHandler(handler)
    logger.info("event=logging_initialized")
    return logger


def _optional_string(value: Any) -> str | None:
    """Normalize a JSON scalar to an optional string."""

    return value if isinstance(value, str) else None


def _string_mapping(value: Any) -> dict[str, str]:
    """Normalize a JSON mapping into a safe string mapping."""

    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}

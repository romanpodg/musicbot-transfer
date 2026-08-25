"""Resumable, observable, confirmation-gated TIDAL library cleanup."""

from __future__ import annotations

import logging
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .auth import ItemUnavailableError, ProgressCallback, TidalClientError, TidalLibraryClient
from .backup import BackupResult, BackupService
from .models import LibrarySnapshot
from .state import DeleteState, DeleteStateStore, atomic_write_json, read_json
from .transfer import ConfirmationRequired


class CleanupScope(StrEnum):
    """The library subsets that can be explicitly removed."""

    FULL = "full"
    TRACKS = "tracks"
    ALBUMS = "albums"
    ARTISTS = "artists"
    PLAYLISTS = "playlists"
    VIDEOS = "videos"
    MIXES = "mixes"


class IncompleteLibraryError(RuntimeError):
    """Raised when a safe cleanup plan cannot determine every target."""


class CleanupStateError(RuntimeError):
    """Raised when a persisted queue and checkpoint cannot be safely reconciled."""


CleanupProgressCallback = Callable[["CleanupProgress"], None]


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    """A reviewed, point-in-time set of library objects to remove."""

    scope: CleanupScope
    snapshot: LibrarySnapshot
    counts: dict[str, int]

    @property
    def total(self) -> int:
        """Return the total number of destructive operations in the plan."""

        return sum(self.counts.values())


@dataclass(frozen=True, slots=True)
class CleanupProgress:
    """A display-safe progress event emitted before and after each item."""

    category: str
    current: int
    total: int
    label: str
    failed: int


@dataclass(frozen=True, slots=True)
class CleanupVerification:
    """Comparison of original targets with a fresh post-cleanup snapshot."""

    counts: dict[str, int]
    new_counts: dict[str, int] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        """Return the number of selected objects that still exist."""

        return sum(self.counts.values())


@dataclass(slots=True)
class CleanupResult:
    """Sanitized outcomes of a confirmed cleanup operation."""

    successful: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    completed: int = 0
    total: int = 0
    interrupted: bool = False
    target_items: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class DeleteQueue:
    """The durable authoritative order and status of cleanup work items."""

    operation: str
    items: list[dict[str, Any]]
    last_applied_seq: int = 0

    @classmethod
    def from_plan(cls, plan: CleanupPlan) -> "DeleteQueue":
        """Build the exact queue before sending the first delete request."""

        items: list[dict[str, Any]] = []
        for category in _sections_for(plan.scope):
            for source in getattr(plan.snapshot, category):
                item_id = str(source.get("id", ""))
                if item_id:
                    items.append(_queue_item(category, item_id, source))
        if "playlists" in _sections_for(plan.scope):
            for source in reversed(plan.snapshot.folders):
                item_id = str(source.get("id", ""))
                if item_id:
                    items.append(_queue_item("folders", item_id, source))
        return cls(operation=f"delete_{plan.scope.value}", items=items)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeleteQueue":
        """Validate persisted queue data without accepting arbitrary statuses."""

        version = value.get("format_version", 1)
        if not isinstance(version, int) or version not in {1, 2}:
            raise CleanupStateError("delete_queue_version_unsupported")

        raw_items = value.get("items")
        if not isinstance(raw_items, list):
            raise CleanupStateError("delete_queue_items_invalid")
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise CleanupStateError("delete_queue_item_invalid")
            status = str(raw.get("status", "pending"))
            if status not in {"pending", "processing", "completed", "already_absent", "failed_retryable", "failed_permanent", "ambiguous"}:
                raise CleanupStateError("delete_queue_status_invalid")
            item_id = str(raw.get("id", ""))
            category = str(raw.get("category", ""))
            if not item_id or category not in _all_queue_categories():
                raise CleanupStateError("delete_queue_item_invalid")
            items.append(
                {
                    "category": category,
                    "id": item_id,
                    "title": str(raw.get("title", "")),
                    "artist": str(raw.get("artist", "")),
                    "is_owned": bool(raw.get("is_owned", False)),
                    "status": status,
                    "attempts": max(0, int(raw.get("attempts", 0))),
                    "reason": str(raw.get("reason", "")),
                }
            )
        return cls(operation=str(value.get("operation", "delete")), items=items, last_applied_seq=max(0, int(value.get("last_applied_seq", 0))))

    def as_dict(self) -> dict[str, Any]:
        """Serialize safe library metadata and no provider credentials."""

        return {"format_version": 2, "operation": self.operation, "last_applied_seq": self.last_applied_seq, "items": self.items}

    def reset_interrupted_items(self) -> None:
        """Return an unacknowledged in-flight request to the pending queue."""

        for item in self.items:
            if item["status"] == "processing":
                item["status"] = "pending"
                item["reason"] = ""

    def reset_failed_items(self) -> None:
        """Allow a user-approved resume to retry bounded API failures."""

        for item in self.items:
            if item["status"] == "failed_retryable":
                item["status"] = "pending"
                item["reason"] = ""

    def statuses(self) -> list[str]:
        """Return queue statuses for aggregate checkpoint calculation."""

        return [str(item["status"]) for item in self.items]


class DeleteQueueStore:
    """Persist the cleanup queue atomically beside the deletion checkpoint."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.journal_path = path.with_suffix(path.suffix + ".journal")

    def exists(self) -> bool:
        """Return whether the durable cleanup queue exists."""

        return self.path.is_file()

    def load(self) -> DeleteQueue:
        """Read and validate a cleanup queue."""

        queue = DeleteQueue.from_dict(read_json(self.path))
        if not self.journal_path.exists():
            return queue
        by_key = {(item["category"], item["id"]): item for item in queue.items}
        try:
            with self.journal_path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
                for index, line in enumerate(lines):
                    event = json.loads(line)
                    seq = event.get("seq")
                    if not isinstance(seq, int):
                        raise CleanupStateError("delete_queue_journal_invalid")
                    if seq <= queue.last_applied_seq:
                        continue
                    if seq != queue.last_applied_seq + 1:
                        raise CleanupStateError("delete_queue_journal_sequence_invalid")
                    key = (str(event["category"]), str(event["id"]))
                    item = by_key.get(key)
                    if item is None or event["status"] not in {"pending", "processing", "completed", "already_absent", "failed_retryable", "failed_permanent", "ambiguous"}:
                        raise CleanupStateError("delete_queue_journal_invalid")
                    item["status"] = event["status"]
                    item["attempts"] = max(0, int(event.get("attempts", item["attempts"])))
                    item["reason"] = str(event.get("reason", ""))
                    queue.last_applied_seq = seq
        except json.JSONDecodeError:
            # A power-loss can truncate the final un-terminated append only.
            if index == len(lines) - 1 and not line.endswith("\n"):
                return queue
            raise CleanupStateError("delete_queue_journal_invalid") from None
        except (OSError, KeyError, TypeError, ValueError):
            raise CleanupStateError("delete_queue_journal_invalid") from None
        return queue

    def save(self, queue: DeleteQueue) -> None:
        """Persist an exact queue after every observable status transition."""

        atomic_write_json(self.path, queue.as_dict())
        self.journal_path.unlink(missing_ok=True)

    def record(self, queue: DeleteQueue, item: dict[str, Any]) -> None:
        """Durably append one O(1) status transition between compactions."""

        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "category": str(item["category"]), "id": str(item["id"]),
            "status": str(item["status"]), "attempts": int(item["attempts"]),
            "reason": str(item.get("reason", "")),
        }
        queue.last_applied_seq += 1
        event["seq"] = queue.last_applied_seq
        with self.journal_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def clear(self) -> None:
        """Remove the queue only after all selected deletions succeeded."""

        self.path.unlink(missing_ok=True)
        self.journal_path.unlink(missing_ok=True)


class CleanupManager:
    """Create safe cleanup plans and process durable deletion queues."""

    def __init__(
        self,
        logger: logging.Logger,
        state_store: DeleteStateStore | None = None,
        queue_store: DeleteQueueStore | None = None,
    ) -> None:
        self._logger = logger
        self._state_store = state_store
        self._queue_store = queue_store

    def estimate_cleanup(
        self,
        client: TidalLibraryClient,
        scope: CleanupScope,
        progress: ProgressCallback | None = None,
    ) -> CleanupPlan:
        """Capture a fresh complete snapshot and calculate exact targets."""

        self._logger.info("event=cleanup_estimate_started scope=%s", scope.value)
        snapshot = client.export_library(progress)
        sections = _sections_for(scope)
        required = set(sections)
        if "playlists" in required:
            required.add("folders")
        incomplete = required.intersection(snapshot.incomplete_sections)
        if incomplete:
            self._logger.error(
                "event=cleanup_estimate_incomplete scope=%s sections=%s",
                scope.value,
                ",".join(sorted(incomplete)),
            )
            raise IncompleteLibraryError("cleanup_targets_incomplete")
        counts = {section: len(getattr(snapshot, section)) for section in sections}
        if "playlists" in sections:
            counts["folders"] = len(snapshot.folders)
        plan = CleanupPlan(scope=scope, snapshot=snapshot, counts=counts)
        self._logger.info(
            "event=cleanup_estimate_completed scope=%s total=%d", scope.value, plan.total
        )
        return plan

    def prepare(self, client: TidalLibraryClient, scope: CleanupScope) -> CleanupPlan:
        """Compatibility name for callers using the original cleanup service."""

        return self.estimate_cleanup(client, scope)

    def create_backup(
        self,
        client: TidalLibraryClient,
        backups: BackupService,
        progress: ProgressCallback | None = None,
    ) -> BackupResult:
        """Create a credential-free backup before a user-approved cleanup."""

        self._logger.info("event=cleanup_backup_started")
        result = backups.create(client, progress=progress)
        self._logger.info("event=cleanup_backup_completed path=%s", result.path.name)
        return result

    def execute(
        self,
        client: TidalLibraryClient,
        plan: CleanupPlan,
        *,
        confirmed: bool,
        progress: CleanupProgressCallback | None = None,
    ) -> CleanupResult:
        """Persist a new queue then delete only after service-layer confirmation."""

        if not confirmed:
            raise ConfirmationRequired("cleanup_confirmation_required")
        self._require_persistence()
        queue = DeleteQueue.from_plan(plan)
        state = DeleteState.create(queue.operation, len(queue.items))
        self._queue_store.save(queue)
        self._state_store.save(state)
        self._logger.info(
            "event=cleanup_started operation=%s total=%d", state.operation, state.total
        )
        return self._process(client, queue, state, progress)

    def resume(
        self,
        client: TidalLibraryClient,
        *,
        confirmed: bool,
        progress: CleanupProgressCallback | None = None,
    ) -> CleanupResult:
        """Continue interrupted work or retry recorded failures after confirmation."""

        if not confirmed:
            raise ConfirmationRequired("cleanup_confirmation_required")
        self._require_persistence()
        state, queue = self._load_persisted()
        self._reconcile_interrupted_items(client, queue)
        if state.finished and state.failed:
            queue.reset_failed_items()
        state.interrupted = False
        state.finished = False
        state.refresh(queue.statuses())
        self._queue_store.save(queue)
        self._state_store.save(state)
        self._logger.info(
            "event=cleanup_resumed operation=%s completed=%d total=%d",
            state.operation,
            state.completed,
            state.total,
        )
        return self._process(client, queue, state, progress)

    def _reconcile_interrupted_items(self, client: TidalLibraryClient, queue: DeleteQueue) -> None:
        """Resolve the remote outcome of a delete recorded as in-flight.

        The queue is journaled before a remote deletion.  If the process dies
        after TIDAL accepted it, a fresh snapshot lets resume mark that target
        complete instead of issuing a blind second mutation.
        """

        in_flight = [item for item in queue.items if item["status"] == "processing"]
        if not in_flight:
            return
        exporter = getattr(client, "export_library", None)
        if not callable(exporter):
            # Test doubles and legacy integrations cannot reconcile.  Favorite
            # deletes remain safe to replay, but production clients always
            # provide export_library.
            queue.reset_interrupted_items()
            return
        snapshot = exporter()
        if snapshot.incomplete_sections:
            raise CleanupStateError("delete_resume_reconciliation_incomplete")
        live_ids = {
            category: {str(record.get("id", "")) for record in getattr(snapshot, category)}
            for category in _all_queue_categories()
        }
        for item in in_flight:
            category, item_id = str(item["category"]), str(item["id"])
            if item_id not in live_ids[category]:
                item["status"] = "completed"
                item["reason"] = "reconciled_absent"
                self._logger.info(
                    "event=cleanup_resume_reconciled category=%s item_id=%s outcome=already_deleted",
                    category, item_id,
                )
            else:
                item["status"] = "pending"
                item["reason"] = ""

    def load_resume_state(self) -> DeleteState | None:
        """Return a saved state only when its matching queue is also present."""

        if self._state_store is None or self._queue_store is None:
            return None
        if not self._state_store.exists() and not self._queue_store.exists():
            return None
        if not self._state_store.exists() or not self._queue_store.exists():
            raise CleanupStateError("delete_state_queue_missing")
        state, _ = self._load_persisted()
        return state

    def delete_tracks(self, client: TidalLibraryClient, item: dict[str, Any]) -> None:
        """Remove one queued favorite track."""

        client.remove_favorite("tracks", str(item["id"]))

    def delete_albums(self, client: TidalLibraryClient, item: dict[str, Any]) -> None:
        """Remove one queued favorite album."""

        client.remove_favorite("albums", str(item["id"]))

    def delete_artists(self, client: TidalLibraryClient, item: dict[str, Any]) -> None:
        """Remove one queued favorite artist."""

        client.remove_favorite("artists", str(item["id"]))

    def delete_videos(self, client: TidalLibraryClient, item: dict[str, Any]) -> None:
        """Remove one queued favorite video."""

        client.remove_favorite("videos", str(item["id"]))

    def delete_mixes(self, client: TidalLibraryClient, item: dict[str, Any]) -> None:
        """Remove one queued favorite mix or radio item."""

        client.remove_favorite("mixes", str(item["id"]))

    def delete_playlists(self, client: TidalLibraryClient, item: dict[str, Any]) -> None:
        """Delete an owned playlist or unfavorite a public playlist."""

        if bool(item.get("is_owned", False)):
            client.delete_playlist(str(item["id"]))
        else:
            client.remove_favorite("playlists", str(item["id"]))

    def verify_cleanup(
        self,
        client: TidalLibraryClient,
        scope: CleanupScope,
        original_targets: list[dict[str, str]] | None = None,
        progress: ProgressCallback | None = None,
    ) -> CleanupVerification:
        """Verify the original target IDs, not whether the library is empty."""

        snapshot = client.export_library(progress)
        sections = _sections_for(scope)
        required = set(sections)
        if "playlists" in required:
            required.add("folders")
        incomplete = required.intersection(snapshot.incomplete_sections)
        if incomplete:
            raise IncompleteLibraryError("cleanup_verification_incomplete")
        targets = original_targets or []
        live_categories = list(sections)
        if "playlists" in sections:
            live_categories.append("folders")
        live_ids = {
            category: {str(item.get("id", "")) for item in getattr(snapshot, category)}
            for category in live_categories
        }
        target_ids: dict[str, set[str]] = {}
        for item in targets:
            category, item_id = str(item.get("category", "")), str(item.get("id", ""))
            if category and item_id:
                target_ids.setdefault(category, set()).add(item_id)
        # Backward-compatible caller fallback: target semantics cannot be
        # reconstructed from a scope alone, so report the fresh count only.
        counts = {
            category: len(target_ids.get(category, set()).intersection(live_ids.get(category, set())))
            if targets else len(live_ids.get(category, set()))
            for category in live_ids
        }
        new_counts = {
            category: len(live_ids.get(category, set()) - target_ids.get(category, set()))
            for category in live_ids
        } if targets else {}
        self._logger.info(
            "event=cleanup_verification_completed scope=%s remaining=%d",
            scope.value,
            sum(counts.values()),
        )
        return CleanupVerification(counts=counts, new_counts=new_counts)

    def _process(
        self,
        client: TidalLibraryClient,
        queue: DeleteQueue,
        state: DeleteState,
        progress: CleanupProgressCallback | None,
    ) -> CleanupResult:
        result = CleanupResult(
            total=state.total,
            target_items=[{"category": str(item["category"]), "id": str(item["id"])} for item in queue.items],
        )
        try:
            for position, item in enumerate(queue.items, start=1):
                if item["status"] in {"completed", "already_absent", "failed_permanent"}:
                    continue
                item["status"] = "processing"
                item["attempts"] = int(item["attempts"]) + 1
                item["reason"] = ""
                state.current_category = str(item["category"])
                state.current_item_id = str(item["id"])
                state.interrupted = False
                self._persist(queue, state, item)
                self._emit_progress(progress, state, item, position)
                self._logger.info(
                    "event=cleanup_deleting category=%s current=%d total=%d item_id=%s label=%s",
                    item["category"], position, state.total, item["id"], _safe_log_value(_label(item)),
                )
                try:
                    self._delete_item(client, item)
                    item["status"] = "completed"
                    result.successful.append(_result_item(item))
                    self._logger.info(
                        "event=cleanup_deleted category=%s item_id=%s label=%s",
                        item["category"], item["id"], _safe_log_value(_label(item)),
                    )
                except ItemUnavailableError:
                    item["status"] = "already_absent"
                    item["reason"] = "already_absent"
                    result.successful.append(_result_item(item, "already_absent"))
                    self._logger.info(
                        "event=cleanup_already_absent category=%s item_id=%s",
                        item["category"], item["id"],
                    )
                except Exception as error:
                    reason = _error_reason(error)
                    item["status"] = "failed_retryable" if reason in {"api_timeout", "network_error", "api_server_error", "rate_limited"} else "failed_permanent"
                    item["reason"] = reason
                    result.failed.append(_result_item(item, reason))
                    self._logger.error(
                        "event=cleanup_failed category=%s item_id=%s reason=%s attempts=%d",
                        item["category"], item["id"], reason, item["attempts"],
                    )
                self._persist(queue, state, item)
                self._emit_progress(progress, state, item, position)
        except KeyboardInterrupt:
            state.interrupted = True
            state.finished = False
            self._persist(queue, state, item)
            self._logger.warning(
                "event=cleanup_interrupted operation=%s completed=%d total=%d",
                state.operation, state.completed, state.total,
            )
            result.completed = state.completed
            result.interrupted = True
            raise

        state.current_category = None
        state.current_item_id = None
        state.interrupted = False
        state.finished = True
        self._persist(queue, state)
        result.completed = state.completed
        result.failed = [
            _result_item(item, str(item.get("reason", "provider_error")))
            for item in queue.items
            if item["status"] in {"failed_retryable", "failed_permanent"}
        ]
        self._logger.info(
            "event=cleanup_finished operation=%s completed=%d failed=%d total=%d",
            state.operation, state.completed, state.failed, state.total,
        )
        if state.failed == 0:
            self._state_store.clear()
            self._queue_store.clear()
        return result

    def _delete_item(self, client: TidalLibraryClient, item: dict[str, Any]) -> None:
        handlers: dict[str, Callable[[TidalLibraryClient, dict[str, Any]], None]] = {
            "tracks": self.delete_tracks,
            "albums": self.delete_albums,
            "artists": self.delete_artists,
            "videos": self.delete_videos,
            "mixes": self.delete_mixes,
            "playlists": self.delete_playlists,
            "folders": self._delete_folder,
        }
        handlers[str(item["category"])](client, item)

    @staticmethod
    def _delete_folder(client: TidalLibraryClient, item: dict[str, Any]) -> None:
        client.delete_folder(str(item["id"]))

    def _persist(
        self, queue: DeleteQueue, state: DeleteState, item: dict[str, Any] | None = None
    ) -> None:
        state.refresh(queue.statuses())
        if item is None:
            self._queue_store.save(queue)
        else:
            self._queue_store.record(queue, item)
        self._state_store.save(state)

    @staticmethod
    def _emit_progress(
        progress: CleanupProgressCallback | None,
        state: DeleteState,
        item: dict[str, Any],
        current: int,
    ) -> None:
        if progress:
            progress(CleanupProgress(str(item["category"]), current, state.total, _label(item), state.failed))

    def _require_persistence(self) -> None:
        if self._state_store is None or self._queue_store is None:
            raise CleanupStateError("cleanup_persistence_not_configured")

    def _load_persisted(self) -> tuple[DeleteState, DeleteQueue]:
        """Load and cross-check persisted cleanup data before a resume mutation."""

        self._require_persistence()
        try:
            state = self._state_store.load()
            queue = self._queue_store.load()
        except (OSError, TypeError, ValueError, CleanupStateError):
            raise CleanupStateError("delete_state_invalid") from None
        if (
            state.operation not in _cleanup_operations()
            or state.operation != queue.operation
            or state.total != len(queue.items)
        ):
            raise CleanupStateError("delete_state_queue_mismatch")
        return state, queue


# Compatibility name for callers built against the original release.
CleanupService = CleanupManager


def _sections_for(scope: CleanupScope) -> tuple[str, ...]:
    if scope is CleanupScope.FULL:
        return ("tracks", "albums", "artists", "videos", "mixes", "playlists")
    return (scope.value,)


def _all_queue_categories() -> set[str]:
    return {"tracks", "albums", "artists", "videos", "mixes", "playlists", "folders"}


def _cleanup_operations() -> set[str]:
    return {f"delete_{scope.value}" for scope in CleanupScope}


def _queue_item(category: str, item_id: str, source: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": category,
        "id": item_id,
        "title": str(source.get("title") or source.get("name") or ""),
        "artist": str(source.get("artist") or ""),
        "is_owned": bool(source.get("is_owned", False)),
        "status": "pending",
        "attempts": 0,
        "reason": "",
    }


def _label(item: dict[str, Any]) -> str:
    title = str(item.get("title", "")).strip()
    artist = str(item.get("artist", "")).strip()
    if artist and title:
        return f"{artist} - {title}"
    return title or str(item.get("id", ""))


def _safe_log_value(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ")[:300]


def _result_item(item: dict[str, Any], reason: str = "") -> dict[str, str]:
    result = {"category": str(item["category"]), "id": str(item["id"])}
    if reason:
        result["reason"] = reason
    return result


def _error_reason(error: Exception) -> str:
    if isinstance(error, TidalClientError):
        return str(error) or "provider_error"
    return "provider_error"

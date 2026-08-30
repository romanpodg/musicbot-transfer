"""JSON-backed implementations of the repository ports.

Storage layout::

    <root>/state/jobs/<job_id>.json     one TransferJob
    <root>/state/items/<job_id>.json    that job's TransferItem list
    <root>/state/plans/<job_id>.json    the latest TransferPlan
    <root>/state/accounts.json          connected accounts

Job and item files are separate so that the per-item checkpointing the executor
performs after every write does not rewrite the whole job document each time.

No file here contains a token or password; accounts store only an
``auth_reference`` pointer to the encrypted credential store.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...core.domain import Account, TransferItem, TransferJob, TransferPlan
from ...core.enums import ItemStatus, JobStatus, Platform
from ...core.errors import PersistenceError
from ...core.ports import (
    AccountRepository,
    TransferItemRepository,
    TransferJobRepository,
    TransferPlanRepository,
    UnitOfWork,
)
from .json_store import JsonDocumentStore, atomic_write_json, ensure_directories, read_json

_LOGGER = logging.getLogger("music_transfer.persistence")


class NullUnitOfWork(UnitOfWork):
    """A no-op transaction boundary for storage without transactions.

    JSON files are replaced atomically, so no rollback is possible; the context
    manager exists so callers can be written correctly today and drop in a real
    database later.
    """

    def __enter__(self) -> NullUnitOfWork:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class JsonTransferJobRepository(TransferJobRepository):
    """Store transfer jobs as one JSON document per job."""

    def __init__(self, root: Path, logger: logging.Logger | None = None) -> None:
        self._root = root / "jobs"
        ensure_directories(root, "jobs")
        self._logger = logger or _LOGGER

    def _path(self, job_id: str) -> Path:
        return self._root / f"{job_id}.json"

    def add(self, job: TransferJob) -> TransferJob:
        path = self._path(job.id)
        if path.is_file():
            raise PersistenceError("job_already_exists")
        atomic_write_json(path, job.as_dict())
        return job

    def get(self, job_id: str) -> TransferJob | None:
        path = self._path(job_id)
        if not path.is_file():
            return None
        return TransferJob.from_dict(read_json(path))

    def update(self, job: TransferJob) -> TransferJob:
        job.touch()
        atomic_write_json(self._path(job.id), job.as_dict())
        return job

    def list_for_user(
        self, user_id: str, *, statuses: tuple[JobStatus, ...] | None = None
    ) -> list[TransferJob]:
        jobs = [
            TransferJob.from_dict(read_json(path))
            for path in sorted(self._root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        ]
        return [
            job
            for job in jobs
            if job.user_id == user_id and (statuses is None or job.status in statuses)
        ]

    def list_all(self) -> list[TransferJob]:
        """Return every stored job, newest first (used by the CLI resume prompt)."""

        return [
            TransferJob.from_dict(read_json(path))
            for path in sorted(self._root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        ]


class JsonTransferItemRepository(TransferItemRepository):
    """Store all of a job's items in one document, rewritten after each change.

    Per-item durability costs a full rewrite, which is acceptable for the local
    CLI (library sizes are in the thousands, not millions).  A database
    implementation will update a single row instead.
    """

    def __init__(self, root: Path, logger: logging.Logger | None = None) -> None:
        self._root = root / "items"
        ensure_directories(root, "items")
        self._logger = logger or _LOGGER

    def _path(self, job_id: str) -> Path:
        return self._root / f"{job_id}.json"

    def load(self, job_id: str) -> list[TransferItem]:
        """Return every stored item for a job, or an empty list."""

        path = self._path(job_id)
        if not path.is_file():
            return []
        payload = read_json(path)
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise PersistenceError("items_invalid")
        return [TransferItem.from_dict(item) for item in raw_items if isinstance(item, dict)]

    def _store(self, job_id: str, items: list[TransferItem]) -> None:
        atomic_write_json(
            self._path(job_id), {"job_id": job_id, "items": [item.as_dict() for item in items]}
        )

    def add_many(self, items: list[TransferItem]) -> list[TransferItem]:
        if not items:
            return []
        job_id = items[0].job_id
        existing = self.load(job_id)
        known = {item.id for item in existing}
        merged = list(existing) + [item for item in items if item.id not in known]
        self._store(job_id, merged)
        return items

    def update(self, item: TransferItem) -> TransferItem:
        items = self.load(item.job_id)
        replaced = False
        for index, stored in enumerate(items):
            if stored.id == item.id:
                items[index] = item
                replaced = True
                break
        if not replaced:
            items.append(item)
        self._store(item.job_id, items)
        return item

    def list_for_job(
        self, job_id: str, *, statuses: tuple[ItemStatus, ...] | None = None
    ) -> list[TransferItem]:
        items = self.load(job_id)
        items.sort(key=lambda item: (item.original_position, item.id))
        if statuses is None:
            return items
        return [item for item in items if item.status in statuses]

    def count_by_status(self, job_id: str) -> dict[str, int]:
        counts = {status.value: 0 for status in ItemStatus}
        for item in self.load(job_id):
            counts[item.status.value] = counts.get(item.status.value, 0) + 1
        return counts


class JsonTransferPlanRepository(TransferPlanRepository):
    """Store the latest plan for each job."""

    def __init__(self, root: Path, logger: logging.Logger | None = None) -> None:
        self._root = root / "plans"
        ensure_directories(root, "plans")
        self._logger = logger or _LOGGER

    def save(self, plan: TransferPlan) -> TransferPlan:
        atomic_write_json(self._root / f"{plan.job_id}.json", plan.as_dict())
        return plan

    def get(self, job_id: str) -> TransferPlan | None:
        path = self._root / f"{job_id}.json"
        if not path.is_file():
            return None
        payload = read_json(path)
        return TransferPlan(
            job_id=str(payload.get("job_id", job_id)),
            source_platform=Platform(str(payload.get("source_platform"))),
            destination_platform=Platform(str(payload.get("destination_platform"))),
            created_at=str(payload.get("created_at", "")),
            items=[TransferItem.from_dict(item) for item in (payload.get("items") or [])],
            warnings=[str(value) for value in (payload.get("warnings") or [])],
            source_incomplete=bool(payload.get("source_incomplete", False)),
            destination_incomplete=bool(payload.get("destination_incomplete", False)),
            metadata=dict(payload.get("metadata") or {}),
        )


class JsonAccountRepository(AccountRepository):
    """Store connected accounts without any credential material."""

    def __init__(self, root: Path, logger: logging.Logger | None = None) -> None:
        ensure_directories(root)
        self._store = JsonDocumentStore(root / "accounts.json")
        self._logger = logger or _LOGGER

    def _all(self) -> list[Account]:
        if not self._store.exists():
            return []
        payload = self._store.read()
        raw = payload.get("accounts")
        if not isinstance(raw, list):
            raise PersistenceError("accounts_invalid")
        return [Account.from_dict(item) for item in raw if isinstance(item, dict)]

    def _save_all(self, accounts: list[Account]) -> None:
        self._store.write({"accounts": [account.as_dict() for account in accounts]})

    def add(self, account: Account) -> Account:
        accounts = self._all()
        if any(existing.id == account.id for existing in accounts):
            raise PersistenceError("account_already_exists")
        accounts.append(account)
        self._save_all(accounts)
        return account

    def get(self, account_id: str) -> Account | None:
        for account in self._all():
            if account.id == account_id:
                return account
        return None

    def find(
        self, platform: Any, platform_account_id: str, owner_user_id: str | None = None
    ) -> Account | None:
        for account in self._all():
            if account.platform != platform:
                continue
            if account.platform_account_id != platform_account_id:
                continue
            if owner_user_id is not None and account.owner_user_id != owner_user_id:
                continue
            return account
        return None

    def list_all(self, owner_user_id: str | None = None) -> list[Account]:
        if owner_user_id is None:
            return self._all()
        return [
            account for account in self._all() if account.owner_user_id == owner_user_id
        ]

    def list_for_user(self, owner_user_id: str) -> list[Account]:
        return [
            account for account in self._all() if account.owner_user_id == owner_user_id
        ]

    def update(self, account: Account) -> Account:
        accounts = self._all()
        for index, existing in enumerate(accounts):
            if existing.id == account.id:
                accounts[index] = account
                self._save_all(accounts)
                return account
        raise PersistenceError("account_not_found")

    def remove(self, account_id: str) -> bool:
        accounts = self._all()
        remaining = [account for account in accounts if account.id != account_id]
        if len(remaining) == len(accounts):
            return False
        self._save_all(remaining)
        return True

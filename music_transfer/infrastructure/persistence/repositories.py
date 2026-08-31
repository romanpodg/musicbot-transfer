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
from ...core.enums import ItemStatus, JobStatus
from ...core.errors import PersistenceError, PlanIntegrityError
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

    def replace_for_job(self, job_id: str, items: list[TransferItem]) -> list[TransferItem]:
        self._store(job_id, list(items))
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
    """Store transfer plans with revision history and immutability checks."""

    def __init__(self, root: Path, logger: logging.Logger | None = None) -> None:
        self._root = root / "plans"
        ensure_directories(root, "plans")
        self._logger = logger or _LOGGER

    def _revision_path(self, job_id: str, revision: int, plan_id: str) -> Path:
        return self._root / f"{job_id}_rev{revision}_{plan_id}.json"

    def _latest_path(self, job_id: str) -> Path:
        return self._root / f"{job_id}.json"

    def save(self, plan: TransferPlan) -> TransferPlan:
        # Invariant: Persisted plan revisions must never be silently overwritten with different content
        if plan.plan_id:
            # Check if this exact plan_id or revision already exists
            pattern = f"{plan.job_id}_rev{plan.revision}_*.json"
            existing_rev_files = list(self._root.glob(pattern))
            for p in existing_rev_files:
                existing = self._load_file(p)
                if existing is not None:
                    if existing.plan_id != plan.plan_id:
                        raise PlanIntegrityError(
                            f"plan_integrity_compromised: duplicate revision {plan.revision} with different plan_id '{existing.plan_id}' vs '{plan.plan_id}'"
                        )
                    if existing.plan_hash != plan.plan_hash or existing.compute_hash() != plan.compute_hash():
                        raise PlanIntegrityError(
                            f"plan_integrity_compromised: plan revision {plan.revision} already exists with different hash"
                        )

            # Check if plan_id exists under any revision
            id_pattern = f"*_rev*_{plan.plan_id}.json"
            for p in self._root.glob(id_pattern):
                existing = self._load_file(p)
                if existing is not None:
                    if existing.job_id != plan.job_id:
                        raise PlanIntegrityError(
                            f"plan_integrity_compromised: plan {plan.plan_id} already exists for different job {existing.job_id}"
                        )
                    if existing.plan_hash != plan.plan_hash:
                        raise PlanIntegrityError(
                            f"plan_integrity_compromised: plan {plan.plan_id} already exists with different hash"
                        )

            rev_path = self._revision_path(plan.job_id, plan.revision, plan.plan_id)
            atomic_write_json(rev_path, plan.as_dict())

        # Update latest pointer
        atomic_write_json(self._latest_path(plan.job_id), plan.as_dict())
        return plan

    def _load_file(self, path: Path) -> TransferPlan | None:
        if not path.is_file():
            return None
        payload = read_json(path)
        return TransferPlan.from_dict(payload)

    def get(self, job_id: str) -> TransferPlan | None:
        return self._load_file(self._latest_path(job_id))

    def get_by_id(self, plan_id: str) -> TransferPlan | None:
        if not plan_id:
            return None
        # Check files matching pattern
        for path in self._root.glob(f"*_rev*_{plan_id}.json"):
            loaded = self._load_file(path)
            if loaded is not None and loaded.plan_id == plan_id:
                return loaded
        # Check latest pointers as fallback
        for path in self._root.glob("*.json"):
            if "_rev" not in path.name:
                loaded = self._load_file(path)
                if loaded is not None and loaded.plan_id == plan_id:
                    return loaded
        return None

    def get_revision(self, job_id: str, revision: int) -> TransferPlan | None:
        matches = list(self._root.glob(f"{job_id}_rev{revision}_*.json"))
        if matches:
            return self._load_file(matches[0])
        latest = self.get(job_id)
        if latest is not None and latest.revision == revision:
            return latest
        return None

    def list_for_job(self, job_id: str) -> list[TransferPlan]:
        plans: dict[int, TransferPlan] = {}
        for path in self._root.glob(f"{job_id}_rev*.json"):
            loaded = self._load_file(path)
            if loaded is not None and loaded.job_id == job_id:
                plans[loaded.revision] = loaded
        if not plans:
            latest = self.get(job_id)
            if latest is not None and latest.job_id == job_id:
                plans[latest.revision] = latest
        return [plans[k] for k in sorted(plans.keys())]


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

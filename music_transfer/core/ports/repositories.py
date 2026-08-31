"""Persistence ports.

The core depends on these interfaces only.  The current implementation is JSON
files; a future PostgreSQL implementation can be added without touching the
transfer engine, the planner, or the executor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..domain import Account, TransferItem, TransferJob, TransferPlan
from ..enums import ItemStatus, JobStatus


class TransferJobRepository(ABC):
    """Durable storage for transfer jobs."""

    @abstractmethod
    def add(self, job: TransferJob) -> TransferJob:
        """Persist a new job and return it."""

    @abstractmethod
    def get(self, job_id: str) -> TransferJob | None:
        """Return a job by id, or ``None`` when it is unknown."""

    @abstractmethod
    def update(self, job: TransferJob) -> TransferJob:
        """Persist changes to an existing job."""

    @abstractmethod
    def list_for_user(
        self, user_id: str, *, statuses: tuple[JobStatus, ...] | None = None
    ) -> list[TransferJob]:
        """Return a user's jobs, newest first, optionally filtered by status."""

    def set_status(self, job: TransferJob, status: JobStatus) -> TransferJob:
        """Update a job's status and persist it.

        Provided here (rather than in the engine) so every caller persists the
        transition the same way.
        """

        from ..transfer.lifecycle import transition

        transition(job, status)
        return self.update(job)


class TransferItemRepository(ABC):
    """Durable storage for transfer items.

    Item-level persistence is what makes resume safe: after a crash the engine
    reads item statuses rather than trusting a single ``last_position``
    counter (Invariant E).
    """

    @abstractmethod
    def add_many(self, items: list[TransferItem]) -> list[TransferItem]:
        """Persist a batch of new items."""

    @abstractmethod
    def update(self, item: TransferItem) -> TransferItem:
        """Persist changes to one item."""

    @abstractmethod
    def list_for_job(
        self, job_id: str, *, statuses: tuple[ItemStatus, ...] | None = None
    ) -> list[TransferItem]:
        """Return a job's items in their original order."""

    @abstractmethod
    def count_by_status(self, job_id: str) -> dict[str, int]:
        """Return per-status counts for a job."""

    def replace_for_job(self, job_id: str, items: list[TransferItem]) -> list[TransferItem]:
        """Replace all items for a job (e.g. during clean re-planning before writes)."""
        return self.add_many(items)


class TransferPlanRepository(ABC):
    """Durable storage for transfer plans (useful for audit and resume)."""

    @abstractmethod
    def save(self, plan: TransferPlan) -> TransferPlan:
        """Persist a plan."""

    @abstractmethod
    def get(self, job_id: str) -> TransferPlan | None:
        """Return the latest plan for a job, or ``None``."""

    @abstractmethod
    def get_by_id(self, plan_id: str) -> TransferPlan | None:
        """Return a plan by its unique plan_id, or ``None``."""

    @abstractmethod
    def get_revision(self, job_id: str, revision: int) -> TransferPlan | None:
        """Return a specific revision of a job's plan, or ``None``."""

    @abstractmethod
    def list_for_job(self, job_id: str) -> list[TransferPlan]:
        """Return all historical revisions of a job's plan sorted by revision."""


class AccountRepository(ABC):
    """Durable storage for connected music-service accounts.

    Implementations must never persist tokens or passwords; only an
    ``auth_reference`` pointer to the encrypted credential store.
    """

    @abstractmethod
    def add(self, account: Account) -> Account:
        """Persist a new account."""

    @abstractmethod
    def get(self, account_id: str) -> Account | None:
        """Return an account by internal id."""

    @abstractmethod
    def find(
        self, platform: Any, platform_account_id: str, owner_user_id: str | None = None
    ) -> Account | None:
        """Return an account by platform identity."""

    @abstractmethod
    def list_all(self, owner_user_id: str | None = None) -> list[Account]:
        """Return every known account, optionally filtered by owner."""

    @abstractmethod
    def list_for_user(self, owner_user_id: str) -> list[Account]:
        """Return every account belonging to one interface user."""

    @abstractmethod
    def update(self, account: Account) -> Account:
        """Replace an existing account record, matched by internal id."""

    @abstractmethod
    def remove(self, account_id: str) -> bool:
        """Delete an account record.  Returns whether one was removed."""


class UnitOfWork(ABC):
    """A transactional boundary for repositories that support one.

    The JSON implementation provides a no-op context manager.  A future
    PostgreSQL implementation will begin a real transaction here, so callers
    can be written correctly today without a database.
    """

    @abstractmethod
    def __enter__(self) -> Any: ...

    @abstractmethod
    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def rollback(self) -> None: ...

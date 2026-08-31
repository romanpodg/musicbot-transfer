"""The explicit transfer lifecycle state machine.

Job status is never a free-form string scattered through the code.  Every
transition must go through :func:`transition`, which rejects illegal moves so
that a bug in a worker surfaces immediately instead of corrupting a job.

Lifecycle::

    CREATED -> AUTHENTICATING -> EXPORTING -> NORMALIZING -> MATCHING
        -> PLANNING -> WAITING_CONFIRMATION -> IMPORTING -> VERIFYING
        -> COMPLETED

    For dry runs where no destination mutation occurs, verification is skipped
    and the job transitions directly:
        IMPORTING -> COMPLETED (verification_status = NOT_RUN)

    PAUSED, FAILED and CANCELLED are the exceptional states.  PAUSED is
    resumable back into IMPORTING; FAILED and CANCELLED are terminal.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..domain import TransferJob
from ..enums import JobStatus
from ..errors import InvalidStateTransition

if TYPE_CHECKING:
    from .executor import ExecutionResult

_LOGGER = logging.getLogger("music_transfer.lifecycle")

#: The authoritative transition table.
TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.CREATED: frozenset(
        {JobStatus.AUTHENTICATING, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.AUTHENTICATING: frozenset(
        {JobStatus.EXPORTING, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.EXPORTING: frozenset(
        {JobStatus.NORMALIZING, JobStatus.PAUSED, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.NORMALIZING: frozenset(
        {JobStatus.MATCHING, JobStatus.PAUSED, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.MATCHING: frozenset(
        {JobStatus.PLANNING, JobStatus.PAUSED, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.PLANNING: frozenset(
        {
            JobStatus.WAITING_CONFIRMATION,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }
    ),
    JobStatus.WAITING_CONFIRMATION: frozenset(
        {
            JobStatus.IMPORTING,
            JobStatus.AUTHENTICATING,
            JobStatus.PLANNING,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }
    ),
    JobStatus.IMPORTING: frozenset(
        {
            JobStatus.VERIFYING,
            # Allowed for completed dry-run executions where destination mutation
            # and verification are intentionally skipped.
            JobStatus.COMPLETED,
            JobStatus.PAUSED,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }
    ),
    JobStatus.VERIFYING: frozenset(
        {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    JobStatus.PAUSED: frozenset(
        {JobStatus.IMPORTING, JobStatus.CANCELLED, JobStatus.FAILED}
    ),
    # Terminal states.
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}

TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    """Return whether ``current -> target`` is a legal transition."""

    return target in TRANSITIONS.get(current, frozenset())


def transition(job: TransferJob, target: JobStatus) -> TransferJob:
    """Move a job to ``target`` in place, rejecting illegal transitions.

    Args:
        job: The job to modify.
        target: The requested state.

    Returns:
        The same job, so callers can chain.

    Raises:
        InvalidStateTransition: If the move is not declared in
            :data:`TRANSITIONS`.
    """

    old_status = job.status
    if not can_transition(old_status, target):
        _LOGGER.warning(
            "event=illegal_job_transition job_id=%s current_status=%s target_status=%s",
            job.id,
            old_status.value,
            target.value,
        )
        raise InvalidStateTransition(
            current=str(old_status), target=str(target)
        )
    job.status = target
    job.touch()
    if is_terminal(target) and job.finished_at is None:
        job.finished_at = job.updated_at
    _LOGGER.info(
        "event=job_transition job_id=%s old_status=%s new_status=%s",
        job.id,
        old_status.value,
        target.value,
    )
    return job


def is_terminal(status: JobStatus) -> bool:
    """Return whether a status is terminal."""

    return status in TERMINAL_STATUSES


def resume_target(job: TransferJob) -> JobStatus | None:
    """Return the state a paused or interrupted job should resume into.

    Returns ``None`` for terminal jobs, which must not be resumed.  A job that
    never reached ``IMPORTING`` restarts from planning: re-planning is
    read-only and cheap, whereas replaying writes is not.
    """

    if is_terminal(job.status):
        return None
    if job.status in {JobStatus.PAUSED, JobStatus.IMPORTING}:
        return JobStatus.IMPORTING
    return JobStatus.PLANNING


def status_after_execution(
    job: TransferJob, outcome: ExecutionResult | Any
) -> JobStatus:
    """Decide which job status follows an execution run.

    This is the authoritative execution-outcome policy:
    - cancelled -> CANCELLED
    - fatal abort -> FAILED
    - dry run -> COMPLETED
    - normal execution -> VERIFYING
    """

    if outcome.cancelled:
        return JobStatus.CANCELLED
    if (
        outcome.aborted
        or getattr(outcome, "abort_error", None)
        or getattr(outcome, "aborted_error", None)
    ):
        return JobStatus.FAILED
    if job.settings.dry_run:
        return JobStatus.COMPLETED
    return JobStatus.VERIFYING

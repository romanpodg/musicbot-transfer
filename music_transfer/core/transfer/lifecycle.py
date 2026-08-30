"""The explicit transfer lifecycle state machine.

Job status is never a free-form string scattered through the code.  Every
transition must go through :func:`transition`, which rejects illegal moves so
that a bug in a worker surfaces immediately instead of corrupting a job.

Lifecycle::

    CREATED -> AUTHENTICATING -> EXPORTING -> NORMALIZING -> MATCHING
        -> PLANNING -> WAITING_CONFIRMATION -> IMPORTING -> VERIFYING
        -> COMPLETED

    PAUSED, FAILED and CANCELLED are the exceptional states.  PAUSED is
    resumable back into IMPORTING; FAILED and CANCELLED are terminal.
"""

from __future__ import annotations

from ..domain import TransferJob
from ..enums import JobStatus
from ..errors import InvalidStateTransition

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
            JobStatus.PLANNING,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }
    ),
    JobStatus.IMPORTING: frozenset(
        {
            JobStatus.VERIFYING,
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

    if not can_transition(job.status, target):
        raise InvalidStateTransition(
            current=str(job.status), target=str(target)
        )
    job.status = target
    job.touch()
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

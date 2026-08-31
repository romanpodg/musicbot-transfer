"""Job lifecycle tests: valid transitions are allowed, invalid ones rejected.

The state machine is the guard against nonsense such as verifying a job that was
never executed, or completing a cancelled one.  An illegal transition must raise
rather than silently change state, because a silent fix-up would let an
interface show a success that never happened.
"""

from __future__ import annotations

import unittest

from music_transfer.core.domain import TransferJob
from music_transfer.core.enums import ContentType, JobStatus, Platform
from music_transfer.core.errors import InvalidStateTransition
from music_transfer.core.transfer.lifecycle import (
    TRANSITIONS,
    can_transition,
    is_terminal,
    resume_target,
    transition,
)


def new_job() -> TransferJob:
    """Return a freshly created job."""

    return TransferJob.create(Platform.TIDAL, Platform.TIDAL)


class ValidTransitions(unittest.TestCase):
    """The happy path from creation to completion."""

    def test_full_path(self) -> None:
        """created -> ... -> completed is a legal walk."""

        job = new_job()
        for target in (
            JobStatus.AUTHENTICATING,
            JobStatus.EXPORTING,
            JobStatus.NORMALIZING,
            JobStatus.MATCHING,
            JobStatus.PLANNING,
            JobStatus.WAITING_CONFIRMATION,
            JobStatus.IMPORTING,
            JobStatus.VERIFYING,
            JobStatus.COMPLETED,
        ):
            with self.subTest(target=target.value):
                transition(job, target)
                self.assertIs(job.status, target)

    def test_pause_and_resume(self) -> None:
        """A running job can pause and return to importing."""

        job = new_job()
        transition(job, JobStatus.AUTHENTICATING)
        transition(job, JobStatus.EXPORTING)
        transition(job, JobStatus.PAUSED)
        transition(job, JobStatus.IMPORTING)
        self.assertIs(job.status, JobStatus.IMPORTING)

    def test_replanning_is_allowed(self) -> None:
        """From confirmation the job may go back to planning."""

        job = new_job()
        transition(job, JobStatus.AUTHENTICATING)
        transition(job, JobStatus.EXPORTING)
        transition(job, JobStatus.NORMALIZING)
        transition(job, JobStatus.MATCHING)
        transition(job, JobStatus.PLANNING)
        transition(job, JobStatus.WAITING_CONFIRMATION)
        transition(job, JobStatus.PLANNING)
        self.assertIs(job.status, JobStatus.PLANNING)

    def test_dry_run_completed_transition(self) -> None:
        """A dry-run job can move directly from IMPORTING to COMPLETED."""

        job = new_job()
        transition(job, JobStatus.AUTHENTICATING)
        transition(job, JobStatus.EXPORTING)
        transition(job, JobStatus.NORMALIZING)
        transition(job, JobStatus.MATCHING)
        transition(job, JobStatus.PLANNING)
        transition(job, JobStatus.WAITING_CONFIRMATION)
        transition(job, JobStatus.IMPORTING)
        transition(job, JobStatus.COMPLETED)
        self.assertIs(job.status, JobStatus.COMPLETED)


class RejectedTransitions(unittest.TestCase):
    """Invalid moves raise instead of quietly "fixing" the state."""

    def test_cannot_skip_to_completed(self) -> None:
        """A brand-new job cannot jump straight to completed."""

        job = new_job()
        with self.assertRaises(InvalidStateTransition):
            transition(job, JobStatus.COMPLETED)
        self.assertIs(job.status, JobStatus.CREATED)

    def test_cannot_verify_before_importing(self) -> None:
        """Verification without execution is rejected."""

        job = new_job()
        with self.assertRaises(InvalidStateTransition):
            transition(job, JobStatus.VERIFYING)

    def test_cannot_import_before_confirmation(self) -> None:
        """Execution requires having waited for confirmation."""

        job = new_job()
        transition(job, JobStatus.AUTHENTICATING)
        transition(job, JobStatus.EXPORTING)
        with self.assertRaises(InvalidStateTransition):
            transition(job, JobStatus.IMPORTING)

    def test_terminal_states_are_final(self) -> None:
        """completed, failed, and cancelled accept no further transition."""

        for final in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            for target in JobStatus:
                with self.subTest(final=final.value, target=target.value):
                    self.assertFalse(can_transition(final, target))

    def test_cannot_resume_a_completed_job(self) -> None:
        """A completed job cannot go back to importing."""

        job = new_job()
        job.status = JobStatus.COMPLETED
        with self.assertRaises(InvalidStateTransition):
            transition(job, JobStatus.IMPORTING)
        self.assertFalse(can_transition(JobStatus.COMPLETED, JobStatus.IMPORTING))

    def test_error_carries_both_states(self) -> None:
        """The exception reports the attempted move, for a useful message."""

        job = new_job()
        with self.assertRaises(InvalidStateTransition) as context:
            transition(job, JobStatus.COMPLETED)
        self.assertEqual(context.exception.current, JobStatus.CREATED.value)
        self.assertEqual(context.exception.target, JobStatus.COMPLETED.value)

    def test_transition_predicate_matches_behaviour(self) -> None:
        """``can_transition`` agrees with ``transition`` for every pair."""

        for current in JobStatus:
            for target in JobStatus:
                job = new_job()
                job.status = current
                allowed = can_transition(current, target)
                try:
                    transition(job, target)
                    succeeded = True
                except InvalidStateTransition:
                    succeeded = False
                self.assertEqual(
                    allowed,
                    succeeded,
                    f"{current.value} -> {target.value} disagreed",
                )


class TerminalAndResumeHelpers(unittest.TestCase):
    """Helpers used by the application service and the recovery screen."""

    def test_terminal_statuses(self) -> None:
        """Only completed, failed, and cancelled are terminal."""

        self.assertTrue(is_terminal(JobStatus.COMPLETED))
        self.assertTrue(is_terminal(JobStatus.FAILED))
        self.assertTrue(is_terminal(JobStatus.CANCELLED))
        self.assertFalse(is_terminal(JobStatus.IMPORTING))
        self.assertFalse(is_terminal(JobStatus.PAUSED))

    def test_resume_target_from_paused(self) -> None:
        """A paused job resumes straight back into importing."""

        job = new_job()
        job.status = JobStatus.PAUSED
        self.assertIs(resume_target(job), JobStatus.IMPORTING)

    def test_resume_target_from_importing(self) -> None:
        """An interrupted import resumes into importing."""

        job = new_job()
        job.status = JobStatus.IMPORTING
        self.assertIs(resume_target(job), JobStatus.IMPORTING)

    def test_resume_target_before_importing_is_planning(self) -> None:
        """A job interrupted before any write re-plans rather than replays.

        Re-planning is read-only; replaying writes risks duplicates, so the
        safe restart point is the plan.
        """

        job = new_job()
        job.status = JobStatus.EXPORTING
        self.assertIs(resume_target(job), JobStatus.PLANNING)

    def test_resume_target_from_terminal_is_none(self) -> None:
        """A finished job has no resume target."""

        job = new_job()
        job.status = JobStatus.COMPLETED
        self.assertIsNone(resume_target(job))

    def test_every_non_terminal_status_has_an_outgoing_edge(self) -> None:
        """No status is a dead end unless it is terminal."""

        for status in JobStatus:
            if is_terminal(status):
                continue
            with self.subTest(status=status.value):
                self.assertTrue(
                    TRANSITIONS[status],
                    f"{status.value} has no outgoing transitions",
                )

    def test_job_is_finished_reflects_terminal_state(self) -> None:
        """``TransferJob.is_finished`` agrees with the transition table."""

        job = new_job()
        self.assertFalse(job.is_finished)
        job.status = JobStatus.COMPLETED
        self.assertTrue(job.is_finished)

    def test_cancellation_is_available_from_every_running_state(self) -> None:
        """A user can always stop a job that is still doing something."""

        for status in JobStatus:
            if is_terminal(status):
                continue
            with self.subTest(status=status.value):
                self.assertTrue(
                    can_transition(status, JobStatus.CANCELLED),
                    f"{status.value} cannot be cancelled",
                )


class JobContentTests(unittest.TestCase):
    """Job creation records what the user actually asked for."""

    def test_requested_content_is_stored(self) -> None:
        """The content tuple is preserved, not collapsed."""

        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            requested_content=(ContentType.PLAYLISTS, ContentType.SAVED_ALBUMS),
        )
        self.assertEqual(
            job.requested_content, (ContentType.PLAYLISTS, ContentType.SAVED_ALBUMS)
        )

    def test_timestamps_are_set(self) -> None:
        """A new job has creation and update timestamps."""

        job = new_job()
        self.assertIsNotNone(job.created_at)
        self.assertIsNotNone(job.updated_at)

    def test_terminal_transition_sets_finished_at(self) -> None:
        """Transitioning to a terminal status stamps finished_at."""

        job = new_job()
        self.assertIsNone(job.finished_at)
        transition(job, JobStatus.CANCELLED)
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(job.finished_at, job.updated_at)


class StatusAfterExecutionTests(unittest.TestCase):
    """Execution finalization outcome policy tests."""

    def test_cancelled_outcome(self) -> None:
        from music_transfer.core.transfer.executor import ExecutionResult
        from music_transfer.core.transfer.lifecycle import status_after_execution

        job = new_job()
        outcome = ExecutionResult(cancelled=True)
        self.assertIs(status_after_execution(job, outcome), JobStatus.CANCELLED)

    def test_aborted_outcome(self) -> None:
        from music_transfer.core.transfer.executor import ExecutionResult
        from music_transfer.core.transfer.lifecycle import status_after_execution

        job = new_job()
        outcome = ExecutionResult(aborted=True, abort_error="fatal_auth")
        self.assertIs(status_after_execution(job, outcome), JobStatus.FAILED)

    def test_dry_run_outcome(self) -> None:
        from music_transfer.core.domain import TransferSettings
        from music_transfer.core.transfer.executor import ExecutionResult
        from music_transfer.core.transfer.lifecycle import status_after_execution

        job = TransferJob.create(
            Platform.TIDAL,
            Platform.SPOTIFY,
            settings=TransferSettings(dry_run=True),
        )
        outcome = ExecutionResult()
        self.assertIs(status_after_execution(job, outcome), JobStatus.COMPLETED)

    def test_normal_outcome(self) -> None:
        from music_transfer.core.transfer.executor import ExecutionResult
        from music_transfer.core.transfer.lifecycle import status_after_execution

        job = new_job()
        outcome = ExecutionResult()
        self.assertIs(status_after_execution(job, outcome), JobStatus.VERIFYING)


if __name__ == "__main__":
    unittest.main()

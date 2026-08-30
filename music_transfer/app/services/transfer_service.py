"""The transfer application service.

This is the single entry point used by every interface (CLI today, Telegram and
workers later).  It owns the lifecycle transitions, the confirmation gate, and
the ordering of phases::

    export -> normalize -> match -> plan -> confirm -> execute -> verify

Nothing here touches a platform SDK or a UI framework: it receives adapters and
repositories and returns domain objects.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ...core.domain import (
    Account,
    LibrarySnapshot,
    TransferItem,
    TransferJob,
    TransferPlan,
    TransferProgress,
    TransferReport,
    TransferSettings,
)
from ...core.enums import ContentType, ItemStatus, JobStatus, Platform
from ...core.errors import ConfirmationRequired, TransferConfigurationError
from ...core.matching import MatchingPolicy, TrackMatcher
from ...core.ports import MusicPlatformAdapter, TransferItemRepository, TransferJobRepository
from ...core.transfer import (
    CancellationToken,
    ExecutionOutcome,
    RecoveryService,
    TransferExecutor,
    TransferPlanner,
    TransferVerifier,
    build_report,
    transition,
)

_LOGGER = logging.getLogger("music_transfer.app.transfer")

ProgressCallback = Callable[[TransferProgress], None]
#: Progress callback used by the export phase: ``(section, current, total)``.
ExportProgress = Callable[[str, int, int], None]

#: Content types that map onto a snapshot section, for partial exports.
_CONTENT_SECTIONS: dict[ContentType, str] = {
    ContentType.LIKED_TRACKS: "tracks",
    ContentType.SAVED_ALBUMS: "albums",
    ContentType.FOLLOWED_ARTISTS: "artists",
    ContentType.PLAYLISTS: "playlists",
}


class TransferService:
    """Orchestrate transfer jobs for any interface."""

    def __init__(
        self,
        jobs: TransferJobRepository,
        items: TransferItemRepository,
        *,
        planner: TransferPlanner | None = None,
        matcher: TrackMatcher | None = None,
        plans_repository: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._jobs = jobs
        self._items = items
        self._plans = plans_repository
        self._matcher = matcher or TrackMatcher()
        self._planner = planner or TransferPlanner(self._matcher, logger)
        self._logger = logger or _LOGGER
        self._recovery = RecoveryService(items, self._logger)

    @property
    def recovery(self) -> RecoveryService:
        """Return the recovery service, used by resume prompts and retry jobs."""

        return self._recovery

    @property
    def jobs(self) -> TransferJobRepository:
        """Return the job repository, so interfaces can list or reload jobs."""

        return self._jobs

    @property
    def items(self) -> TransferItemRepository:
        """Return the item repository, so interfaces can read durable progress."""

        return self._items

    # -- creation ----------------------------------------------------------

    def create_job(
        self,
        source: Account | Platform,
        destination: Account | Platform,
        *,
        content: tuple[ContentType, ...] = (ContentType.LIKED_TRACKS,),
        settings: TransferSettings | None = None,
        user_id: str | None = None,
    ) -> TransferJob:
        """Create and persist a job, rejecting meaningless configurations.

        Raises:
            TransferConfigurationError: When source and destination are the same
                account.  ``TIDAL A -> TIDAL B`` is allowed, so the check is on
                *account identity*, not on platform equality.
        """

        source_account = source if isinstance(source, Account) else None
        destination_account = destination if isinstance(destination, Account) else None
        if (
            source_account is not None
            and destination_account is not None
            and source_account.same_identity(destination_account)
        ):
            raise TransferConfigurationError("same_account_transfer")
        source_platform = source_account.platform if source_account else Platform(source)
        destination_platform = (
            destination_account.platform if destination_account else Platform(destination)
        )
        job = TransferJob.create(
            source_platform,
            destination_platform,
            user_id=user_id,
            requested_content=content,
            settings=settings,
            source_account_id=source_account.id if source_account else None,
            destination_account_id=destination_account.id if destination_account else None,
        )
        job.source_account_label = source_account.label if source_account else None
        job.destination_account_label = (
            destination_account.label if destination_account else None
        )
        self._jobs.add(job)
        self._logger.info(
            "event=job_created job_id=%s source=%s destination=%s content=%s",
            job.id,
            job.source_platform.value,
            job.destination_platform.value,
            ",".join(item.value for item in content),
        )
        return job

    # -- analysis (read-only) ----------------------------------------------

    def analyze(
        self,
        job: TransferJob,
        source: MusicPlatformAdapter,
        destination: MusicPlatformAdapter,
        *,
        export_progress: ExportProgress | None = None,
        snapshot: LibrarySnapshot | None = None,
    ) -> TransferPlan:
        """Export the source and build a plan.  Performs no destination write.

        Returns:
            The persisted :class:`TransferPlan`.

        Raises:
            ConfirmationRequired: Never - analysis is read-only and needs no
                confirmation.  The confirmation gate sits on :meth:`execute`.
        """

        transition(job, JobStatus.AUTHENTICATING)
        self._jobs.update(job)
        transition(job, JobStatus.EXPORTING)
        self._jobs.update(job)
        if snapshot is None:
            snapshot = source.export_library(progress=export_progress)
        self._logger.info(
            "event=source_exported job_id=%s counts=%s partial=%s",
            job.id,
            snapshot.counts(),
            snapshot.is_partial,
        )
        transition(job, JobStatus.NORMALIZING)
        transition(job, JobStatus.MATCHING)
        transition(job, JobStatus.PLANNING)
        self._jobs.update(job)
        result = self._planner.build(job, snapshot, destination)
        self._items.add_many(result.items)
        if self._plans is not None:
            self._plans.save(result.plan)
        job.total_items = result.summary.total_items
        job.touch()
        transition(job, JobStatus.WAITING_CONFIRMATION)
        self._jobs.update(job)
        if snapshot.is_partial:
            self._logger.warning(
                "event=plan_source_partial job_id=%s sections=%s",
                job.id,
                ",".join(snapshot.incomplete_sections),
            )
        return result.plan

    # -- execution ---------------------------------------------------------

    def execute(
        self,
        job: TransferJob,
        destination: MusicPlatformAdapter,
        *,
        confirmed: bool,
        progress: ProgressCallback | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """Execute a planned job and verify the result.

        Args:
            job: The job to run.
            destination: The destination adapter.
            confirmed: Must be ``True``.  Confirmation is an interface concern,
                but the service refuses to write without it so a forgotten UI
                check cannot mutate a library.
            progress: Optional progress sink.
            cancellation_token: Optional cooperative cancellation handle.

        Returns:
            A dictionary with ``report``, ``verification``, and ``outcome``.

        Raises:
            ConfirmationRequired: If ``confirmed`` is ``False``.
        """

        if not confirmed:
            raise ConfirmationRequired("transfer_confirmation_required")
        if job.status is not JobStatus.WAITING_CONFIRMATION:
            # A resumed job comes back from PAUSED or a crashed IMPORTING run.
            if job.status not in {JobStatus.PAUSED, JobStatus.IMPORTING}:
                raise ConfirmationRequired("job_not_ready_for_execution")
        else:
            transition(job, JobStatus.IMPORTING)
        job.status = JobStatus.IMPORTING
        if job.started_at is None:
            job.started_at = job.updated_at
        self._jobs.update(job)
        items = self._items.list_for_job(job.id)
        executor = TransferExecutor(
            destination, self._items, logger=self._logger, cancellation_token=cancellation_token
        )
        outcome = executor.execute(job, items, progress=progress)
        self._jobs.update(job)
        verification: dict[str, Any] = {}
        if outcome.aborted or outcome.abort_error:
            # Fatal failure (e.g. auth/authorization loss). Do not verify or mark completed.
            transition(job, JobStatus.FAILED)
            job.error_code = outcome.abort_error
            job.finished_at = job.updated_at
            self._jobs.update(job)
        elif outcome.cancelled:
            # Cancellation stops cleanly without verification.
            transition(job, JobStatus.CANCELLED)
            job.finished_at = job.updated_at
            self._jobs.update(job)
        elif job.settings.dry_run:
            job.status = JobStatus.COMPLETED
            job.finished_at = job.updated_at
            self._jobs.update(job)
        else:
            transition(job, JobStatus.VERIFYING)
            self._jobs.update(job)
            verifier = TransferVerifier(destination, logger=self._logger)
            results = verifier.verify_job(job, self._items.list_for_job(job.id))
            verification = verifier.as_report(results)
            job.status = JobStatus.COMPLETED
            job.finished_at = job.updated_at
            self._jobs.update(job)
        report = build_report(job, self._items.list_for_job(job.id), outcome)
        report.finish()
        self._logger.info(
            "event=job_finished job_id=%s status=%s transferred=%d failed=%d skipped=%d",
            job.id,
            job.status.value,
            report.transferred,
            report.failed,
            report.skipped,
        )
        return {"report": report, "verification": verification, "outcome": outcome}

    # -- resume and retry --------------------------------------------------

    def resume(
        self,
        job: TransferJob,
        destination: MusicPlatformAdapter,
        *,
        confirmed: bool,
        progress: ProgressCallback | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """Continue an interrupted job from persisted item state.

        Already-completed items are never replayed (Invariant E); ambiguous
        items are resolved against destination state before execution so a
        possibly-landed write is not duplicated.
        """

        if not confirmed:
            raise ConfirmationRequired("transfer_confirmation_required")
        if job.is_finished:
            raise TransferConfigurationError("job_already_finished")
        try:
            state = destination.get_destination_state()
        except Exception as error:  # noqa: BLE001 - reconciliation is best-effort
            self._logger.warning(
                "event=resume_state_unavailable job_id=%s error_type=%s",
                job.id,
                type(error).__name__,
            )
            state = None
        if state is not None:
            self._recovery.resolve_ambiguous(job.id, state)
        job.status = JobStatus.IMPORTING
        job.cancellation_requested = False
        self._jobs.update(job)
        return self.execute(
            job,
            destination,
            confirmed=True,
            progress=progress,
            cancellation_token=cancellation_token,
        )

    def create_retry_job(
        self,
        job: TransferJob,
        statuses: tuple[ItemStatus, ...] | None = None,
    ) -> TransferJob | None:
        """Create a follow-up job containing only the items worth retrying.

        Returns ``None`` when there is nothing to retry.  The original job is
        left untouched so its history remains auditable.
        """

        selected = self._recovery.select_for_retry(job.id, statuses)
        if not selected:
            return None
        retry_job = TransferJob.create(
            job.source_platform,
            job.destination_platform,
            user_id=job.user_id,
            requested_content=job.requested_content,
            settings=job.settings,
            source_account_id=job.source_account_id,
            destination_account_id=job.destination_account_id,
        )
        retry_job.source_account_label = job.source_account_label
        retry_job.destination_account_label = job.destination_account_label
        retry_job.metadata["retry_of"] = job.id
        self._jobs.add(retry_job)
        cloned: list[TransferItem] = []
        for item in selected:
            clone = TransferItem.create(
                retry_job.id,
                item.entity_type,
                item.source_platform,
                item.source_id,
                item.destination_platform,
                original_position=item.original_position,
                container_source_id=item.container_source_id,
                source_metadata=dict(item.source_metadata),
                operation=item.operation,
            )
            clone.destination_id = item.destination_id
            clone.match_method = item.match_method
            clone.match_score = item.match_score
            clone.container_destination_id = item.container_destination_id
            cloned.append(clone)
        self._items.add_many(cloned)
        retry_job.total_items = len(cloned)
        self._jobs.update(retry_job)
        self._logger.info(
            "event=retry_job_created job_id=%s source_job=%s items=%d",
            retry_job.id,
            job.id,
            len(cloned),
        )
        return retry_job

    def request_cancellation(self, job: TransferJob) -> TransferJob:
        """Ask a running job to stop at the next safe boundary.

        Already transferred content is never rolled back automatically.
        """

        job.cancellation_requested = True
        self._jobs.update(job)
        self._logger.info("event=cancellation_requested job_id=%s", job.id)
        return job

    def cancel(self, job: TransferJob) -> TransferJob:
        """Mark a job cancelled and persist it."""

        from ...core.transfer.lifecycle import can_transition

        if can_transition(job.status, JobStatus.CANCELLED):
            transition(job, JobStatus.CANCELLED)
        else:
            job.status = JobStatus.CANCELLED
            job.touch()
        job.finished_at = job.updated_at
        return self._jobs.update(job)

    def fail(self, job: TransferJob, error_code: str) -> TransferJob:
        """Mark a job failed and persist it."""

        from ...core.transfer.lifecycle import can_transition

        job.error_code = error_code
        if can_transition(job.status, JobStatus.FAILED):
            transition(job, JobStatus.FAILED)
        else:
            job.status = JobStatus.FAILED
            job.touch()
        job.finished_at = job.updated_at
        return self._jobs.update(job)

    def report_for(self, job: TransferJob) -> TransferReport:
        """Rebuild a report from persisted item state.

        A report is always derived from durable items, so it can be regenerated
        after a crash or in a different process than the one that ran the job.
        """

        return TransferReport.from_items(job.id, self._items.list_for_job(job.id))

    def matching_policy(self) -> MatchingPolicy:
        """Return the matcher's policy (used by diagnostics and tests)."""

        return self._matcher.policy


def content_sections(content: tuple[ContentType, ...]) -> tuple[str, ...]:
    """Return the snapshot sections required by a set of content types."""

    sections = {
        _CONTENT_SECTIONS[item] for item in content if item in _CONTENT_SECTIONS
    }
    if ContentType.PLAYLISTS in content:
        sections.add("folders")
    return tuple(sorted(sections))


__all__ = [
    "ExecutionOutcome",
    "TransferService",
    "content_sections",
]

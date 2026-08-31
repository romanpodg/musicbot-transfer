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
    new_identifier,
    utc_now,
)
from ...core.enums import (
    ContentType,
    EntityType,
    ItemStatus,
    JobStatus,
    MutationState,
    Platform,
    PreconditionExpectation,
    TransferOperation,
    VerificationStatus,
)
from ...core.errors import (
    AuthenticationError,
    AuthorizationError,
    ConfirmationRequired,
    InvalidPersistedStateError,
    InvalidStateTransition,
    PlanConfirmationMismatch,
    PlanIntegrityError,
    PlanStaleError,
    PlanValidationUnavailableError,
    TransferConfigurationError,
)
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
    scrub_credentials,
    status_after_execution,
    transition,
)
from ...infrastructure.persistence import JsonTransferPlanRepository

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
        if plans_repository is None and hasattr(jobs, "_root"):
            root = jobs._root.parent
            self._plans = JsonTransferPlanRepository(root)
        else:
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

    @property
    def plans(self) -> Any:
        """Return the plan repository."""

        return self._plans

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

        existing_items = self._items.list_for_job(job.id)
        has_started_execution = any(
            it.status is ItemStatus.TRANSFERRED or it.mutation_state is MutationState.IN_FLIGHT
            for it in existing_items
        )
        if has_started_execution:
            raise TransferConfigurationError("cannot_replan_after_writes_started")

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
        plan = result.plan

        # Determine next revision from durable repository (Section 15)
        next_revision = 1
        if self._plans is not None:
            latest = self._plans.get(job.id)
            if latest is not None and latest.revision:
                next_revision = latest.revision + 1

        plan.plan_id = new_identifier("plan")
        plan.revision = next_revision
        plan.plan_hash = plan.compute_hash()

        if hasattr(self._items, "replace_for_job"):
            self._items.replace_for_job(job.id, result.items)
        else:
            self._items.add_many(result.items)
        if self._plans is not None:
            self._plans.save(plan)

        # Update active plan identity on TransferJob and clear previous confirmation (Invariant D)
        job.active_plan_id = plan.plan_id
        job.active_plan_revision = plan.revision
        job.active_plan_hash = plan.plan_hash
        job.confirmed_plan_id = None
        job.confirmed_plan_revision = None
        job.confirmed_plan_hash = None
        job.confirmed_at = None

        job.total_items = result.summary.total_items
        job.touch()
        transition(job, JobStatus.WAITING_CONFIRMATION)
        self._jobs.update(job)

        self._logger.info(
            "event=plan_created job_id=%s plan_id=%s revision=%d hash_prefix=%s",
            job.id,
            plan.plan_id,
            plan.revision,
            plan.plan_hash[:8] if plan.plan_hash else "",
        )

        if snapshot.is_partial:
            self._logger.warning(
                "event=plan_source_partial job_id=%s sections=%s",
                job.id,
                ",".join(snapshot.incomplete_sections),
            )
        return plan

    # -- confirmation ------------------------------------------------------

    def confirm_plan(
        self,
        job: TransferJob,
        *,
        plan_id: str,
        revision: int,
        plan_hash: str,
    ) -> None:
        """Confirm an exact plan revision before execution.

        Invariant C: A plan confirmation applies only to the exact plan_id + revision + plan_hash.
        """
        if self._plans is None:
            raise PlanIntegrityError("plan_repository_unavailable")

        # Load active plan
        plan = self._plans.get_by_id(plan_id) or self._plans.get(job.id)
        if plan is None or not plan.verify_integrity():
            raise PlanIntegrityError("plan_integrity_compromised")

        if (
            job.active_plan_id != plan_id
            or job.active_plan_revision != revision
            or job.active_plan_hash != plan_hash
        ):
            raise PlanConfirmationMismatch("plan_confirmation_mismatch")

        if plan.plan_id != plan_id or plan.revision != revision or plan.plan_hash != plan_hash:
            raise PlanConfirmationMismatch("plan_confirmation_mismatch")

        job.confirmed_plan_id = plan_id
        job.confirmed_plan_revision = revision
        job.confirmed_plan_hash = plan_hash
        job.confirmed_at = utc_now()
        self._jobs.update(job)

        self._logger.info(
            "event=plan_confirmed job_id=%s plan_id=%s revision=%d",
            job.id,
            plan_id,
            revision,
        )

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
            confirmed: Must be ``True``. Confirmation is an interface concern,
                but the service refuses to write without it so a forgotten UI
                check cannot mutate a library.
            progress: Optional progress sink.
            cancellation_token: Optional cooperative cancellation handle.

        Returns:
            A dictionary with ``report``, ``verification``, and ``outcome``.

        Raises:
            ConfirmationRequired: If ``confirmed`` is ``False`` or no durable confirmation exists.
            PlanStaleError: If plan is stale or preconditions/intent drifted.
            PlanIntegrityError: If plan failed integrity verification.
        """

        if not confirmed:
            raise ConfirmationRequired("transfer_confirmation_required")

        current_items = self._items.list_for_job(job.id)
        from ...core.transfer.planner import validate_plan_write_positions
        validate_plan_write_positions(current_items)

        # Check if execution already started (durable writes / in flight)
        has_started_execution = any(
            it.status is ItemStatus.TRANSFERRED or it.mutation_state is MutationState.IN_FLIGHT
            for it in current_items
        )

        # ---------------------------------------------------------------------
        # Preflight Safety Checks (Section 25-39, 44, Invariants A, B, C, F, H, I, K, L)
        # ---------------------------------------------------------------------
        # Section 44: Narrow backward-compatible resume policy for legacy jobs
        # that were already IMPORTING before Phase 1.3B with confirmed evidence
        # of started execution (TRANSFERRED or IN_FLIGHT items).
        is_legacy_resuming_job = (
            job.status is JobStatus.IMPORTING
            and has_started_execution
            and not job.confirmed_plan_id
        )

        if is_legacy_resuming_job:
            self._logger.info(
                "event=legacy_resume_mode job_id=%s reason=confirmed_prior_execution",
                job.id,
            )
            plan = None
        else:
            # Check durable confirmation exists (Invariant A)
            if not job.confirmed_plan_id:
                raise ConfirmationRequired("transfer_confirmation_required")

            # Confirmation must match active plan identity
            if (
                job.confirmed_plan_id != job.active_plan_id
                or job.confirmed_plan_revision != job.active_plan_revision
                or job.confirmed_plan_hash != job.active_plan_hash
            ):
                job.confirmed_plan_id = None
                job.confirmed_plan_revision = None
                job.confirmed_plan_hash = None
                job.confirmed_at = None
                job.error_code = "plan_stale"
                self._jobs.update(job)
                self._logger.warning("event=plan_stale reason=active_plan_mismatch job_id=%s", job.id)
                raise PlanStaleError("plan_stale")

            # Load plan from repository and verify integrity (Invariant F)
            if self._plans is None:
                raise PlanIntegrityError("plan_repository_unavailable")

            plan = self._plans.get_by_id(job.confirmed_plan_id)
            if plan is None:
                plan = self._plans.get(job.id)

            if plan is None or not plan.verify_integrity():
                raise PlanIntegrityError("plan_integrity_compromised")

            # Legacy unversioned plan check (Section 42-43)
            if not plan.plan_id or not plan.plan_hash or plan.revision <= 0:
                raise ConfirmationRequired("transfer_confirmation_required")

            # Verify plan matches job confirmation
            if (
                plan.plan_id != job.confirmed_plan_id
                or plan.revision != job.confirmed_plan_revision
                or plan.plan_hash != job.confirmed_plan_hash
            ):
                job.confirmed_plan_id = None
                job.confirmed_plan_revision = None
                job.confirmed_plan_hash = None
                job.confirmed_at = None
                job.error_code = "plan_stale"
                self._jobs.update(job)
                raise PlanStaleError("plan_stale")

            # Material job context drift check (Sections 7-9, 15)
            context_drift = False
            plan_settings = plan.metadata.get("settings")
            if plan_settings is not None:
                if job.settings.as_dict() != plan_settings:
                    context_drift = True
            else:
                if "ordering" in plan.metadata and str(job.settings.ordering) != str(plan.metadata["ordering"]):
                    context_drift = True
                if "dry_run" in plan.metadata and bool(job.settings.dry_run) != bool(plan.metadata["dry_run"]):
                    context_drift = True

            plan_req_content = plan.metadata.get("requested_content")
            if plan_req_content is not None:
                current_req_content = [c.value for c in job.requested_content]
                if current_req_content != list(plan_req_content):
                    context_drift = True

            if (
                "source_account_id" in plan.metadata
                and job.source_account_id != plan.metadata.get("source_account_id")
            ):
                context_drift = True

            if (
                "destination_account_id" in plan.metadata
                and job.destination_account_id != plan.metadata.get("destination_account_id")
            ):
                context_drift = True

            if job.source_platform != plan.source_platform or job.destination_platform != plan.destination_platform:
                context_drift = True

            if context_drift:
                job.confirmed_plan_id = None
                job.confirmed_plan_revision = None
                job.confirmed_plan_hash = None
                job.confirmed_at = None
                job.error_code = "plan_stale"
                self._jobs.update(job)
                self._logger.warning("event=plan_stale reason=job_context_drift job_id=%s", job.id)
                raise PlanStaleError("plan_stale")

            # Execution item exact membership and order check (Sections 2-6)
            if not has_started_execution:
                item_drift = False
                if len(current_items) != len(plan.items):
                    item_drift = True
                else:
                    for pi, ci in zip(plan.items, current_items, strict=True):
                        if pi.intent_payload() != ci.intent_payload():
                            item_drift = True
                            break

                if item_drift:
                    job.confirmed_plan_id = None
                    job.confirmed_plan_revision = None
                    job.confirmed_plan_hash = None
                    job.confirmed_at = None
                    job.error_code = "plan_stale"
                    self._jobs.update(job)
                    self._logger.warning("event=plan_stale reason=execution_intent_drift job_id=%s", job.id)
                    raise PlanStaleError("plan_stale")

            # Destination precondition validation (Sections 10-13)
            if not has_started_execution and plan.preconditions:
                try:
                    dest_state = destination.get_destination_state()
                except (AuthenticationError, AuthorizationError) as err:
                    # Fatal auth error (Invariant K, Section 38)
                    transition(job, JobStatus.FAILED)
                    job.error_code = err.code
                    job.verification_status = VerificationStatus.NOT_RUN
                    job.finished_at = job.updated_at
                    self._jobs.update(job)
                    raise
                except Exception as err:
                    raise PlanValidationUnavailableError("plan_validation_unavailable") from err

                if dest_state is None:
                    raise PlanValidationUnavailableError("plan_validation_unavailable")

                for pre in plan.preconditions:
                    if not dest_state.is_trustworthy(pre.section):
                        raise PlanValidationUnavailableError("plan_validation_unavailable")

                    if pre.section == "tracks":
                        actual = dest_state.has_track(pre.destination_id)
                    elif pre.section == "albums":
                        actual = pre.destination_id in dest_state.album_ids
                    elif pre.section == "artists":
                        actual = pre.destination_id in dest_state.artist_ids
                    elif pre.section == "playlists":
                        actual = pre.destination_id in dest_state.playlist_ids
                    else:
                        raise InvalidPersistedStateError(
                            "invalid_persisted_state",
                            f"Invalid precondition section: '{pre.section}'",
                        )

                    if pre.expected == PreconditionExpectation.PRESENT or pre.expected == "present":
                        expected = True
                    elif pre.expected == PreconditionExpectation.ABSENT or pre.expected == "absent":
                        expected = False
                    else:
                        raise InvalidPersistedStateError(
                            "invalid_persisted_state",
                            f"Invalid precondition expectation: '{pre.expected}'",
                        )

                    if actual != expected:
                        job.confirmed_plan_id = None
                        job.confirmed_plan_revision = None
                        job.confirmed_plan_hash = None
                        job.confirmed_at = None
                        job.error_code = "plan_stale"
                        self._jobs.update(job)
                        self._logger.warning(
                            "event=plan_stale reason=destination_drift job_id=%s destination_id=%s expected=%s actual=%s",
                            job.id,
                            pre.destination_id,
                            expected,
                            actual,
                        )
                        raise PlanStaleError("plan_stale")


        if job.status is not JobStatus.IMPORTING:
            transition(job, JobStatus.IMPORTING)
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
        next_status = status_after_execution(job, outcome)
        if next_status is JobStatus.FAILED:
            # Fatal failure (e.g. auth/authorization loss). Do not verify or mark completed.
            transition(job, JobStatus.FAILED)
            job.error_code = outcome.abort_error
            job.verification_status = VerificationStatus.NOT_RUN
            job.finished_at = job.updated_at
            self._jobs.update(job)
        elif next_status is JobStatus.CANCELLED:
            # Cancellation stops cleanly without verification.
            transition(job, JobStatus.CANCELLED)
            job.verification_status = VerificationStatus.NOT_RUN
            job.finished_at = job.updated_at
            self._jobs.update(job)
        elif next_status is JobStatus.COMPLETED:
            # Dry run execution finishes without destination mutation or verification.
            transition(job, JobStatus.COMPLETED)
            job.verification_status = VerificationStatus.NOT_RUN
            job.finished_at = job.updated_at
            self._jobs.update(job)
        elif next_status is JobStatus.VERIFYING:
            transition(job, JobStatus.VERIFYING)
            self._jobs.update(job)
            try:
                verifier = TransferVerifier(destination, logger=self._logger)
                results = verifier.verify_job(job, self._items.list_for_job(job.id))
                verification = verifier.as_report(results)
                verif_status = verifier.aggregate_status(
                    results, verification_attempted=True
                )
                job.verification_status = verif_status
                if not results:
                    self._logger.warning(
                        "event=verification_empty job_id=%s reason=nothing_verifiable",
                        job.id,
                    )
                if verif_status is VerificationStatus.PASSED:
                    self._logger.info(
                        "event=verification_passed job_id=%s verification_status=%s",
                        job.id,
                        verif_status.value,
                    )
                elif verif_status is VerificationStatus.FAILED:
                    self._logger.warning(
                        "event=verification_failed job_id=%s verification_status=%s",
                        job.id,
                        verif_status.value,
                    )
                elif verif_status is VerificationStatus.PARTIAL:
                    self._logger.warning(
                        "event=verification_partial job_id=%s verification_status=%s",
                        job.id,
                        verif_status.value,
                    )
                transition(job, JobStatus.COMPLETED)
                job.finished_at = job.updated_at
                self._jobs.update(job)
            except (AuthenticationError, AuthorizationError) as error:
                self._logger.error(
                    "event=verification_fatal_error job_id=%s error_type=%s error_code=%s",
                    job.id,
                    type(error).__name__,
                    error.code,
                )
                transition(job, JobStatus.FAILED)
                job.error_code = error.code
                job.verification_status = VerificationStatus.NOT_RUN
                job.finished_at = job.updated_at
                self._jobs.update(job)
                verification = {}
            except Exception as error:  # noqa: BLE001
                self._logger.error(
                    "event=verification_unexpected_error job_id=%s error_type=%s",
                    job.id,
                    type(error).__name__,
                )
                transition(job, JobStatus.FAILED)
                job.error_code = "verification_error"
                job.verification_status = VerificationStatus.NOT_RUN
                job.finished_at = job.updated_at
                self._jobs.update(job)
                verification = {}
        report = build_report(job, self._items.list_for_job(job.id), outcome)
        if job.verification_status is VerificationStatus.PARTIAL and not verification:
            report.warnings.append("nothing_verifiable")
        report.finish()
        self._logger.info(
            "event=job_finished job_id=%s status=%s verification_status=%s transferred=%d failed=%d skipped=%d",
            job.id,
            job.status.value,
            job.verification_status.value,
            report.transferred,
            report.failed,
            report.skipped,
        )
        return {
            "report": report,
            "verification": verification,
            "verification_status": job.verification_status,
            "outcome": outcome,
        }

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
        from ...core.transfer.lifecycle import resume_target

        target = resume_target(job)
        if target is not JobStatus.IMPORTING:
            raise InvalidStateTransition(
                current=str(job.status), target=str(JobStatus.IMPORTING)
            )
        try:
            state = destination.get_destination_state()
        except (AuthenticationError, AuthorizationError) as error:
            self._logger.error(
                "event=resume_fatal_auth_error job_id=%s error_type=%s error_code=%s",
                job.id,
                type(error).__name__,
                error.code,
            )
            transition(job, JobStatus.FAILED)
            job.error_code = error.code
            job.verification_status = VerificationStatus.NOT_RUN
            job.finished_at = job.updated_at
            self._jobs.update(job)
            outcome = ExecutionOutcome(
                aborted=True,
                abort_error=error.code,
                abort_reason=scrub_credentials(error.message),
            )
            report = build_report(job, self._items.list_for_job(job.id), outcome)
            report.finish()
            return {
                "report": report,
                "verification": {},
                "verification_status": job.verification_status,
                "outcome": outcome,
            }
        except Exception as error:  # noqa: BLE001 - non-auth reconciliation is best-effort
            self._logger.warning(
                "event=resume_state_unavailable job_id=%s error_type=%s",
                job.id,
                type(error).__name__,
            )
            state = None
        if state is not None:
            self._recovery.resolve_ambiguous(job.id, state)
        if job.status is not JobStatus.IMPORTING:
            transition(job, JobStatus.IMPORTING)
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
            # Playlist items cannot execute stale write positions without explicit re-planning.
            # Clear write_position so the retry job requires re-planning before execution.
            if item.entity_type is EntityType.PLAYLIST_ITEM or item.operation is TransferOperation.ADD_PLAYLIST_ITEM:
                clone.write_position = None
            else:
                clone.write_position = item.write_position
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
        """Mark a job cancelled and persist it.

        Idempotent: cancelling an already-cancelled job is a no-op.
        Cancelling a COMPLETED or FAILED job raises InvalidStateTransition.
        """

        if job.status is JobStatus.CANCELLED:
            return job
        transition(job, JobStatus.CANCELLED)
        job.finished_at = job.updated_at
        return self._jobs.update(job)

    def fail(self, job: TransferJob, error_code: str) -> TransferJob:
        """Mark a job failed and persist it.

        Idempotent: failing an already-failed job is a no-op and preserves
        the original failure state unless previously unset.
        Failing a COMPLETED or CANCELLED job raises InvalidStateTransition.
        """

        if job.status is JobStatus.FAILED:
            if not job.error_code:
                job.error_code = error_code
                self._jobs.update(job)
            return job
        job.error_code = error_code
        transition(job, JobStatus.FAILED)
        job.finished_at = job.updated_at
        return self._jobs.update(job)

    def report_for(self, job: TransferJob) -> TransferReport:
        """Rebuild a report from persisted item state.

        A report is always derived from durable items, so it can be regenerated
        after a crash or in a different process than the one that ran the job.
        """

        return TransferReport.from_items(
            job.id,
            self._items.list_for_job(job.id),
            verification_status=job.verification_status,
        )

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

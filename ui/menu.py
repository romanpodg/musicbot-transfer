"""The interactive application controller for TIDAL Library Manager."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from core.auth import (
    AccountRole,
    AuthenticationError,
    CredentialStore,
    TidalAuthenticator,
    TidalLibraryClient,
)
from core.backup import BackupFormatError, BackupResult, BackupService
from core.cleanup import (
    CleanupManager,
    CleanupProgress,
    CleanupResult,
    CleanupScope,
    CleanupStateError,
    DeleteQueueStore,
    IncompleteLibraryError,
)
from core.models import LibrarySnapshot, TransferReport
from core.retry import RetryEvent
from core.sorting import SortOrder
from core.state import (
    ApplicationConfig,
    DeleteStateStore,
    TransferState,
    TransferStateStore,
)
from core.transfer import ConfirmationRequired, TransferOptions, TransferService
from core.verification import VerificationService
from localization.manager import LocalizationManager

from .progress import Console, ProgressRenderer
from .prompts import Prompts


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    """Absolute paths used by the interactive application."""

    root: Path

    @property
    def config(self) -> Path:
        """Return the non-secret configuration path."""

        return self.root / "config.json"

    @property
    def backup(self) -> Path:
        """Return the required default backup path."""

        return self.root / "data" / "backups" / "tidal_backup.json"

    @property
    def state(self) -> Path:
        """Return the resumable transfer checkpoint path."""

        return self.root / "data" / "state" / "transfer_state.json"

    @property
    def delete_state(self) -> Path:
        """Return the durable cleanup resume checkpoint path."""

        return self.root / "data" / "state" / "delete_state.json"

    @property
    def delete_queue(self) -> Path:
        """Return the durable cleanup queue path."""

        return self.root / "data" / "state" / "delete_queue.json"

    @property
    def report(self) -> Path:
        """Return the required verification report path."""

        return self.root / "data" / "reports" / "transfer_report.json"


class TidalManagerApplication:
    """Coordinate localization, authorization, safety prompts, and core services."""

    def __init__(
        self,
        paths: ApplicationPaths,
        config: ApplicationConfig,
        i18n: LocalizationManager,
        logger: logging.Logger,
        dry_run: bool = False,
    ) -> None:
        self._paths = paths
        self._config = config
        self._i18n = i18n
        self._logger = logger
        self._dry_run = dry_run
        self._console = Console(i18n)
        self._prompts = Prompts(self._console)
        self._progress = ProgressRenderer(self._console)
        self._auth = TidalAuthenticator(CredentialStore(logger), logger)
        self._backups = BackupService(paths.backup, logger)
        self._state_store = TransferStateStore(paths.state)
        self._transfers = TransferService(self._state_store, logger)
        self._cleanup = CleanupManager(
            logger,
            DeleteStateStore(paths.delete_state),
            DeleteQueueStore(paths.delete_queue),
        )
        self._verification = VerificationService(paths.report, logger)

    def run(self) -> None:
        """Run the interactive menu until the user exits."""
        try:
            self._choose_initial_language()
            self._console.message("app.banner", style="heading")
            if self._dry_run:
                self._console.message("app.dry_run_enabled", style="warning")
            self._console.blank()
            self._resume_if_requested()
            self._resume_cleanup_if_requested()
            while True:
                self._console.blank()
                choice = self._prompts.choose(
                    "menu.choose",
                    {
                        "1": "menu.transfer",
                        "2": "menu.backup",
                        "3": "menu.restore",
                        "4": "menu.cleanup",
                        "5": "menu.account",
                        "6": "menu.settings",
                        "0": "menu.exit",
                    },
                )
                if choice == "0":
                    self._console.message("app.goodbye")
                    return
                handlers = {
                    "1": self._transfer_library,
                    "2": self._create_backup,
                    "3": self._restore_backup,
                    "4": self._clean_library,
                    "5": self._account_information,
                    "6": self._settings,
                }
                handlers[choice]()
        except KeyboardInterrupt:
            self._safe_shutdown()

    def _choose_initial_language(self) -> None:
        if self._config.language in self._i18n.available_languages():
            self._i18n.set_language(self._config.language)
            return
        language = self._prompts.choose_language(self._i18n.available_languages())
        self._i18n.set_language(language)
        self._config.language = language
        self._config.save(self._paths.config)

    def _resume_if_requested(self) -> None:
        if not self._state_store.exists():
            return
        try:
            state = self._state_store.load()
            operation = self._operation_label(state.operation)
        except Exception as error:
            self._logger.error("event=resume_state_invalid error_type=%s", type(error).__name__)
            self._console.message("transfer.state_invalid", style="error")
            return
        self._console.message("transfer.resume_detected", operation=operation, style="warning")
        if self._dry_run:
            self._console.message("app.dry_run_mutation_blocked", style="warning")
            return
        if not self._prompts.yes_no("transfer.resume_question"):
            self._console.message("transfer.resume_declined")
            return
        destination = self._authenticate(AccountRole.DESTINATION)
        if destination is None:
            return
        account = self._account_name(destination)
        action = self._i18n.t(
            "confirmation.resume_action", count=state.source_snapshot.counts()["tracks"]
        )
        if not self._prompts.confirm_mutation(account, action):
            self._console.message("transfer.cancelled")
            return
        options = TransferOptions(sort_order=self._state_sort_order(state))
        report = self._run_transfer(destination, state, options)
        if report is not None:
            self._complete_transfer(destination, state, report)

    def _resume_cleanup_if_requested(self) -> None:
        """Offer a confirmed continuation whenever a durable cleanup queue exists."""

        try:
            state = self._cleanup.load_resume_state()
        except CleanupStateError as error:
            self._logger.error("event=cleanup_resume_state_invalid code=%s", error.args[:1])
            self._console.message("cleanup.state_invalid", style="error")
            return
        if state is None:
            return
        self._console.message("cleanup.resume_detected", style="warning")
        self._console.message(
            "cleanup.resume_progress", completed=state.completed, total=state.total
        )
        if self._dry_run:
            self._console.message("app.dry_run_mutation_blocked", style="warning")
            return
        if not self._prompts.yes_no("cleanup.resume_question"):
            self._console.message("cleanup.resume_declined")
            return
        client = self._authenticate(AccountRole.SOURCE)
        if client is None:
            return
        summary = self._i18n.t("cleanup.target_summary", count=state.total)
        action = self._i18n.t("confirmation.resume_cleanup_action", count=state.total)
        if not self._prompts.confirm_mutation(self._account_name(client), action):
            self._console.message("cleanup.cancelled")
            return
        if not self._prompts.confirm_deletion(self._account_name(client), summary):
            self._console.message("cleanup.cancelled")
            return
        self._console.message("cleanup.deleting_started", style="heading")
        try:
            result = self._cleanup.resume(client, confirmed=True, progress=self._on_cleanup_progress)
            self._progress.finish()
            self._display_cleanup_result(
                client, result, self._cleanup_scope_from_operation(state.operation)
            )
        except KeyboardInterrupt:
            self._progress.finish()
            raise
        except Exception as error:
            self._progress.finish()
            self._log_and_display_error(error)

    def _safe_shutdown(self) -> None:
        """Finish terminal rendering and show the persisted resumable checkpoint."""

        self._progress.finish()
        self._console.message("shutdown.stopping", style="warning")
        self._console.message("shutdown.saving_state")
        try:
            cleanup_state = self._cleanup.load_resume_state()
        except CleanupStateError:
            cleanup_state = None
        if cleanup_state is not None:
            self._console.message(
                "shutdown.progress_saved",
                completed=cleanup_state.completed,
                total=cleanup_state.total,
            )
        elif self._state_store.exists():
            try:
                state = self._state_store.load()
                completed = sum(len(items) for items in state.completed_objects.values())
                total = sum(state.source_snapshot.counts().values())
                self._console.message(
                    "shutdown.progress_saved", completed=completed, total=total
                )
            except Exception:
                self._logger.error("event=shutdown_transfer_state_unreadable")
        self._logger.warning("event=application_interrupted")
        self._console.message("shutdown.continue_later")

    def _on_retry(self, event: RetryEvent) -> None:
        """Present provider retry events using only localized, safe reason codes."""

        self._console.message("retry.attempt", attempt=event.attempt, total=event.max_attempts)
        self._console.message(
            "retry.retrying",
            style="warning",
            reason=self._i18n.t(f"retry.reason.{event.reason}"),
            seconds=f"{event.delay_seconds:g}",
        )

    def _transfer_library(self) -> None:
        source = self._authenticate(AccountRole.SOURCE)
        if source is None:
            return
        source_snapshot = self._export_snapshot(source)
        if source_snapshot is None:
            return
        if source_snapshot.incomplete_sections:
            self._console.message("transfer.source_incomplete", style="warning")
            return
        destination = self._authenticate(AccountRole.DESTINATION)
        if destination is None:
            return
        self._console.message("summary.source", style="heading")
        self._console.library_summary(self._account_name(source), source_snapshot.counts())
        self._console.blank()
        self._console.message("summary.destination", style="heading")
        self._console.message("summary.account", account=self._account_name(destination))
        options = TransferOptions(sort_order=self._choose_sort_order())
        if self._dry_run:
            self._console.message("app.dry_run_mutation_blocked", style="warning")
            return
        action = self._i18n.t(
            "confirmation.transfer_action", count=source_snapshot.counts()["tracks"]
        )
        if not self._prompts.confirm_mutation(self._account_name(destination), action):
            self._console.message("transfer.cancelled")
            return
        state = TransferState.create("transfer", source_snapshot, options.sort_order.value)
        report = self._run_transfer(destination, state, options)
        if report is not None:
            self._complete_transfer(destination, state, report)

    def _create_backup(self) -> None:
        client = self._authenticate(AccountRole.SOURCE)
        if client is None:
            return
        self._console.message("backup.creating", style="heading")
        try:
            result = self._backups.create(
                client,
                progress=lambda category, current, total: self._progress.update(
                    category, current, total, operation="backup"
                ),
            )
            self._progress.finish()
            self._display_backup_result(result)
        except Exception as error:
            self._progress.finish()
            self._log_and_display_error(error)

    def _restore_backup(self) -> None:
        backups = self._backups.list_backups()
        if not backups:
            self._console.message("backup.none")
            return
        selection = self._prompts.choose_values(
            "prompt.backup_choose",
            {str(index): path.name for index, path in enumerate(backups, start=1)},
        )
        path = backups[int(selection) - 1]
        try:
            source_snapshot = self._backups.load(path)
        except BackupFormatError:
            self._console.message("backup.invalid", style="error")
            return
        if source_snapshot.incomplete_sections:
            self._console.message("transfer.source_incomplete", style="warning")
            return
        destination = self._authenticate(AccountRole.DESTINATION)
        if destination is None:
            return
        self._console.message(
            "restore.summary", account=self._display_name(source_snapshot)
        )
        self._console.library_summary(self._display_name(source_snapshot), source_snapshot.counts())
        if self._dry_run:
            self._console.message("app.dry_run_mutation_blocked", style="warning")
            return
        action = self._i18n.t(
            "confirmation.restore_action", count=source_snapshot.counts()["tracks"]
        )
        if not self._prompts.confirm_mutation(self._account_name(destination), action):
            self._console.message("restore.cancelled")
            return
        state = TransferState.create("restore", source_snapshot, SortOrder.ORIGINAL.value)
        report = self._run_transfer(
            destination, state, TransferOptions(sort_order=SortOrder.ORIGINAL)
        )
        if report is not None:
            self._complete_transfer(destination, state, report)

    def _clean_library(self) -> None:
        """Estimate, optionally back up, then safely process a queued cleanup."""

        selection = self._prompts.choose(
            "prompt.cleanup_choose",
            {
                "1": "cleanup.full",
                "2": "cleanup.tracks",
                "3": "cleanup.albums",
                "4": "cleanup.artists",
                "5": "cleanup.playlists",
                "6": "cleanup.videos",
                "7": "cleanup.mixes",
                "0": "cleanup.cancel",
            },
        )
        if selection == "0":
            return
        scope = {
            "1": CleanupScope.FULL,
            "2": CleanupScope.TRACKS,
            "3": CleanupScope.ALBUMS,
            "4": CleanupScope.ARTISTS,
            "5": CleanupScope.PLAYLISTS,
            "6": CleanupScope.VIDEOS,
            "7": CleanupScope.MIXES,
        }[selection]
        client = self._authenticate(AccountRole.SOURCE)
        if client is None:
            return
        self._console.message("cleanup.preparing", style="heading")
        try:
            plan = self._cleanup.estimate_cleanup(
                client,
                scope,
                progress=lambda category, current, total: self._progress.update(
                    category, current, total, operation="cleanup"
                ),
            )
        except IncompleteLibraryError:
            self._console.message("cleanup.incomplete", style="error")
            return
        except Exception as error:
            self._log_and_display_error(error)
            return
        finally:
            self._progress.finish()
        if self._dry_run:
            self._console.message("app.dry_run_enabled", style="warning")
            self._console.message("cleanup.dry_run_would_delete")
            self._console.counts(plan.counts)
            self._console.message("cleanup.dry_run_no_changes", style="success")
            self._logger.info("event=cleanup_dry_run scope=%s total=%d", scope.value, plan.total)
            return
        if self._prompts.yes_no("cleanup.offer_backup"):
            self._console.message("backup.creating", style="heading")
            try:
                self._display_backup_result(
                    self._cleanup.create_backup(
                        client,
                        self._backups,
                        progress=lambda category, current, total: self._progress.update(
                            category, current, total, operation="backup"
                        ),
                    )
                )
            except Exception as error:
                self._log_and_display_error(error)
                return
            finally:
                self._progress.finish()
        summary = self._i18n.t("cleanup.target_summary", count=plan.total)
        if not self._prompts.confirm_deletion(self._account_name(client), summary):
            self._console.message("cleanup.cancelled")
            return
        self._console.message("cleanup.deleting_started", style="heading")
        try:
            result = self._cleanup.execute(
                client,
                plan,
                confirmed=True,
                progress=self._on_cleanup_progress,
            )
            self._progress.finish()
            self._display_cleanup_result(client, result, scope)
        except KeyboardInterrupt:
            self._progress.finish()
            raise
        except ConfirmationRequired:
            self._progress.finish()
            self._console.message("errors.confirmation_required", style="error")
        except Exception as error:
            self._progress.finish()
            self._log_and_display_error(error)

    def _account_information(self) -> None:
        client = self._authenticate(AccountRole.SOURCE)
        if client is None:
            return
        snapshot = self._export_snapshot(client)
        if snapshot is None:
            return
        self._console.message("account.information", style="heading")
        self._console.library_summary(self._account_name(client), snapshot.counts())
        if snapshot.incomplete_sections:
            self._console.message(
                "account.library_incomplete",
                style="warning",
                sections=self._section_labels(snapshot.incomplete_sections),
            )

    def _settings(self) -> None:
        while True:
            choice = self._prompts.choose(
                "menu.settings_choose",
                {
                    "1": "menu.language",
                    "2": "menu.disconnect_source",
                    "3": "menu.disconnect_destination",
                    "0": "menu.back",
                },
            )
            if choice == "0":
                return
            if choice == "1":
                language = self._prompts.choose_language(self._i18n.available_languages())
                self._i18n.set_language(language)
                self._config.language = language
                self._config.save(self._paths.config)
                self._console.message("settings.language_saved", style="success")
            elif choice == "2":
                self._auth.forget(AccountRole.SOURCE)
                self._console.message("settings.source_forgotten", style="success")
            else:
                self._auth.forget(AccountRole.DESTINATION)
                self._console.message("settings.destination_forgotten", style="success")

    def _authenticate(self, role: AccountRole) -> TidalLibraryClient | None:
        self._console.message("account.loading", style="heading")
        try:
            client = self._auth.connect(role, self._console.message)
            client.set_retry_callback(self._on_retry)
            return client
        except AuthenticationError as error:
            self._logger.warning("event=authentication_displayed_error code=%s", error.args[:1])
            if error.args and error.args[0] == "tidalapi_missing":
                self._console.message("errors.tidalapi_missing", style="error")
            else:
                self._console.message("errors.authentication_failed", style="error")
            return None

    def _export_snapshot(self, client: TidalLibraryClient) -> LibrarySnapshot | None:
        try:
            snapshot = client.export_library(
                progress=lambda category, current, total: self._progress.update(
                    category, current, total, operation="verification"
                )
            )
            self._progress.finish()
            return snapshot
        except Exception as error:
            self._progress.finish()
            self._log_and_display_error(error)
            return None

    def _run_transfer(
        self, destination: TidalLibraryClient, state: TransferState, options: TransferOptions
    ) -> TransferReport | None:
        try:
            report = self._transfers.run(
                destination,
                state,
                options,
                confirmed=True,
                progress=lambda category, current, total: self._progress.update(
                    category, current, total, operation="transfer"
                ),
            )
            self._progress.finish()
            return report
        except KeyboardInterrupt:
            self._progress.finish()
            raise
        except ConfirmationRequired:
            self._progress.finish()
            self._console.message("errors.confirmation_required", style="error")
            return None
        except Exception as error:
            self._progress.finish()
            self._log_and_display_error(error)
            return None

    def _complete_transfer(
        self, destination: TidalLibraryClient, state: TransferState, report: TransferReport
    ) -> None:
        try:
            self._verification.verify_and_write(
                state.source_snapshot,
                destination,
                report,
                progress=lambda category, current, total: self._progress.update(
                    category, current, total, operation="verification"
                ),
            )
            self._progress.finish()
        except Exception as error:
            self._progress.finish()
            self._logger.error("event=verification_failed error_type=%s", type(error).__name__)
            self._console.message("errors.operation_failed", style="error")
            return
        self._console.message(
            "transfer.report_written", path=self._relative_path(self._paths.report)
        )
        if report.has_retryable_failures():
            self._console.message("transfer.partial", style="warning")
        else:
            self._state_store.clear()
            self._console.message("transfer.completed", style="success")

    def _on_cleanup_progress(self, event: CleanupProgress) -> None:
        """Render every queued deletion with current object and error count."""

        self._progress.update(
            event.category,
            event.current,
            event.total,
            item=event.label,
            errors=event.failed,
            operation="cleanup",
        )

    def _display_cleanup_result(
        self,
        client: TidalLibraryClient,
        result: CleanupResult,
        scope: CleanupScope,
    ) -> None:
        """Show aggregate deletion outcome and verify only a clean queue result."""

        completed = result.completed
        failed_items = result.failed
        self._console.message(
            "cleanup.completed",
            style="success" if not failed_items else "warning",
            successful=completed,
            failed=len(failed_items),
        )
        if failed_items:
            return
        try:
            verification = self._cleanup.verify_cleanup(
                client,
                scope,
                progress=lambda category, current, total: self._progress.update(
                    category, current, total, operation="verification"
                ),
            )
            self._progress.finish()
            self._console.message(
                "cleanup.verify_remaining",
                style="success" if verification.remaining == 0 else "warning",
                count=verification.remaining,
            )
        except IncompleteLibraryError:
            self._progress.finish()
            self._console.message("cleanup.verification_unavailable", style="warning")
        except Exception as error:
            self._progress.finish()
            self._log_and_display_error(error)

    @staticmethod
    def _cleanup_scope_from_operation(operation: str) -> CleanupScope:
        """Map persisted deletion operations back to their selected scope."""

        value = operation.removeprefix("delete_")
        return CleanupScope(value)

    def _choose_sort_order(self) -> SortOrder:
        choice = self._prompts.choose(
            "prompt.sort_choose",
            {
                "1": "sort.newest",
                "2": "sort.oldest",
                "3": "sort.alphabetical",
                "4": "sort.artist",
                "5": "sort.album",
                "6": "sort.original",
            },
        )
        return {
            "1": SortOrder.NEWEST_FIRST,
            "2": SortOrder.OLDEST_FIRST,
            "3": SortOrder.ALPHABETICAL,
            "4": SortOrder.ARTIST,
            "5": SortOrder.ALBUM,
            "6": SortOrder.ORIGINAL,
        }[choice]

    def _display_backup_result(self, result: BackupResult) -> None:
        self._console.message(
            "backup.completed", style="success", path=self._relative_path(result.path)
        )
        if result.is_partial:
            self._console.message(
                "backup.partial",
                style="warning",
                sections=self._section_labels(result.snapshot.incomplete_sections),
            )

    def _operation_label(self, operation: str) -> str:
        if operation not in ("transfer", "restore"):
            return self._i18n.t("operation.transfer")
        return self._i18n.t(f"operation.{operation}")

    def _state_sort_order(self, state: TransferState) -> SortOrder:
        try:
            return SortOrder(state.sort_order)
        except ValueError:
            return SortOrder.ORIGINAL

    def _account_name(self, client: TidalLibraryClient) -> str:
        profile = client.profile()
        return profile.display_name or self._i18n.t("account.unknown")

    def _display_name(self, snapshot: LibrarySnapshot) -> str:
        return snapshot.account.display_name or self._i18n.t("account.unknown")

    def _section_labels(self, sections: list[str]) -> str:
        return ", ".join(self._i18n.t(f"category.{section}") for section in sections)

    def _relative_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._paths.root))
        except ValueError:
            return str(path)

    def _log_and_display_error(self, error: Exception) -> None:
        self._logger.error("event=operation_failed error_type=%s", type(error).__name__)
        self._console.message("errors.operation_failed", style="error")

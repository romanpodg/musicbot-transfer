"""Dependency wiring for the CLI.

This is the single place where concrete implementations are chosen.  Every
command receives a fully wired :class:`CliContext`, so swapping JSON files for
PostgreSQL later is a change here and nowhere else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...app.services import (
    AccountService,
    DiagnosticsService,
    TransferService,
)
from ...config import Settings, ensure_data_directories, load_dotenv
from ...core.enums import Platform
from ...core.matching import MatchingPolicy, TrackMatcher
from ...core.transfer import TransferPlanner
from ...infrastructure.http import RetryPolicy
from ...infrastructure.logging import configure_logging
from ...infrastructure.persistence import (
    JsonAccountRepository,
    JsonTransferItemRepository,
    JsonTransferJobRepository,
    JsonTransferPlanRepository,
)
from ...infrastructure.security.credentials import CredentialStore
from ...locales import LocalizationManager
from ...platforms.registry import PlatformRegistry, default_registry
from .console import Console, ProgressRenderer
from .prompts import Prompts

_LOGGER = logging.getLogger("music_transfer.cli")


@dataclass(slots=True)
class CliContext:
    """Everything a command needs, already wired and ready."""

    settings: Settings
    logger: logging.Logger
    i18n: LocalizationManager
    console: Console
    prompts: Prompts
    progress: ProgressRenderer
    registry: PlatformRegistry
    accounts: AccountService
    transfers: TransferService
    diagnostics: DiagnosticsService

    def save_language(self, language: str) -> None:
        """Persist the language preference and switch the active catalog."""

        self.i18n.set_language(language)
        self.settings.save_language(language)

    def connect(
        self, platform: Platform, role: str = "source"
    ) -> Any:
        """Connect an account, streaming OAuth messages to the console."""

        return self.accounts.connect(
            platform, role, emit=lambda message: self.console.text(message)
        )


def build_context(
    project_root: Path,
    *,
    language: str | None = None,
    log_level: str | None = None,
    configure_logs: bool = True,
    input_function: Any = None,
    output: Any = None,
    registry: PlatformRegistry | None = None,
) -> CliContext:
    """Build a fully wired CLI context.

    Args:
        project_root: Repository/data root.
        language: Override the persisted language preference.
        log_level: Override the configured log level.
        configure_logs: Set to ``False`` in tests to avoid touching log files.
        input_function: Injectable ``input`` replacement (testing).
        output: Injectable ``print`` replacement (testing).
        registry: Injectable platform registry (testing).
    """

    load_dotenv(project_root / ".env")
    settings = Settings.load(project_root)
    ensure_data_directories(settings)
    level = (log_level or settings.log_level or "INFO").upper()
    if configure_logs:
        logger = configure_logging(settings.logs / "music_transfer.log", level)
    else:
        # Tests and one-off scripts must not create or rotate the real log file.
        logger = logging.getLogger("music_transfer")
        logger.setLevel(level)
    i18n = LocalizationManager(language=language or settings.language or "en")
    i18n.validate_catalogs()
    console = Console(i18n, output=output)
    prompts = Prompts(console, input_function=input_function)
    resolved_registry = registry or default_registry()

    store_root = settings.state
    jobs = JsonTransferJobRepository(store_root, logger)
    item_repo = JsonTransferItemRepository(store_root, logger)
    plans = JsonTransferPlanRepository(store_root, logger)
    account_repository = JsonAccountRepository(store_root, logger)

    policy = MatchingPolicy(
        high_confidence=settings.matching.high_confidence,
        ambiguous_threshold=settings.matching.ambiguous_threshold,
        fuzzy_enabled=settings.matching.fuzzy_enabled,
        max_candidates=settings.matching.max_candidates,
    )
    matcher = TrackMatcher(policy)
    planner = TransferPlanner(matcher, logger)
    transfers = TransferService(
        jobs, item_repo, planner=planner, matcher=matcher, plans_repository=plans, logger=logger
    )
    accounts = AccountService(
        account_repository,
        registry=resolved_registry,
        authenticators=_build_authenticators(logger, settings),
        logger=logger,
    )
    diagnostics = DiagnosticsService(
        settings, registry=resolved_registry, accounts=accounts, logger=logger
    )
    return CliContext(
        settings=settings,
        logger=logger,
        i18n=i18n,
        console=console,
        prompts=prompts,
        progress=ProgressRenderer(console),
        registry=resolved_registry,
        accounts=accounts,
        transfers=transfers,
        diagnostics=diagnostics,
    )


def _build_authenticators(
    logger: logging.Logger, settings: Settings
) -> dict[Platform, Any]:
    """Build the platform authenticators that are safe to construct here.

    TIDAL's authenticator imports ``tidalapi`` lazily, so an environment without
    the SDK still boots - connecting simply fails with a clear error instead of
    an ``ImportError`` at startup.
    """

    retry_policy = RetryPolicy(
        timeout_seconds=settings.http.timeout_seconds,
        max_attempts=settings.http.max_attempts,
        initial_backoff_seconds=settings.http.initial_backoff_seconds,
        max_backoff_seconds=settings.http.max_backoff_seconds,
    )
    authenticators: dict[Platform, Any] = {}
    try:
        from ...platforms.tidal.auth import TidalAuthenticator

        authenticators[Platform.TIDAL] = TidalAuthenticator(
            CredentialStore(logger), logger, retry_policy
        )
    except ImportError as error:
        logger.warning(
            "event=authenticator_unavailable platform=tidal error_type=%s",
            type(error).__name__,
        )
    return authenticators


__all__ = ["CliContext", "build_context"]

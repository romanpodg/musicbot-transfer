"""CLI commands.

Each function is a thin translation from parsed arguments to application-service
calls.  There is no transfer logic here: planning, matching, execution,
verification, and recovery all live in the core and are reached through
:class:`~music_transfer.app.services.TransferService`.

Two rules are enforced structurally rather than by convention:

* :func:`command_plan` performs no destination write - the planner wraps the
  destination adapter in :class:`ReadOnlyAdapter`;
* :func:`command_transfer` refuses to run unless a confirmation decision was
  collected first (``--yes`` counts, an interactive prompt counts).
"""

from __future__ import annotations

import json
from typing import Any

from ...app.dto import plan_view, report_view, verification_views
from ...core.domain import TransferSettings
from ...core.enums import ContentType, OrderingMode, Platform
from ...core.errors import (
    ConfirmationRequired,
    MusicTransferError,
    TransferConfigurationError,
)
from .context import CliContext

#: Maps CLI-friendly content names onto the typed enum.
CONTENT_CHOICES: dict[str, ContentType] = {
    "tracks": ContentType.LIKED_TRACKS,
    "albums": ContentType.SAVED_ALBUMS,
    "artists": ContentType.FOLLOWED_ARTISTS,
    "playlists": ContentType.PLAYLISTS,
    "videos": ContentType.VIDEOS,
    "mixes": ContentType.MIXES,
}

#: Maps CLI-friendly ordering names onto the typed enum.
ORDERING_CHOICES: dict[str, OrderingMode] = {
    "newest": OrderingMode.DATE_ADDED_NEWEST_FIRST,
    "oldest": OrderingMode.DATE_ADDED_OLDEST_FIRST,
    "alphabetical": OrderingMode.ALPHABETICAL,
    "artist": OrderingMode.ARTIST,
    "album": OrderingMode.ALBUM,
    "original": OrderingMode.SOURCE_ORDER,
}


def command_diagnostics(context: CliContext) -> int:
    """Print environment and adapter diagnostics."""

    context.console.diagnostics(context.diagnostics.run())
    return 0


def command_accounts(context: CliContext) -> int:
    """Print the connected / not-connected / not-implemented account screen."""

    context.console.accounts(context.accounts.statuses())
    return 0


def command_plan(context: CliContext, arguments: Any) -> int:
    """Build and display a transfer plan without writing anything."""

    source = _require_connection(context, _platform(arguments.source), "source")
    destination = _require_connection(context, _platform(arguments.destination), "destination")
    if source is None or destination is None:
        return 2
    settings = _settings_from_arguments(arguments)
    job = context.transfers.create_job(
        source, destination, content=_content(arguments), settings=settings
    )
    plan = context.transfers.analyze(
        job,
        _adapter(context, source, "source"),
        _adapter(context, destination, "destination"),
        export_progress=_export_progress(context),
    )
    context.console.blank()
    context.console.plan(plan_view(job, plan))
    context.console.blank()
    context.console.message("job.job_line", job_id=job.id, status=job.status.value)
    return 0


def command_transfer(context: CliContext, arguments: Any) -> int:
    """Plan, collect confirmation, execute, and verify."""

    source = _require_connection(context, _platform(arguments.source), "source")
    destination = _require_connection(context, _platform(arguments.destination), "destination")
    if source is None or destination is None:
        return 2
    settings = _settings_from_arguments(arguments)
    job = context.transfers.create_job(
        source, destination, content=_content(arguments), settings=settings
    )
    source_adapter = _adapter(context, source, "source")
    destination_adapter = _adapter(context, destination, "destination")
    plan = context.transfers.analyze(
        job, source_adapter, destination_adapter, export_progress=_export_progress(context)
    )
    context.console.blank()
    context.console.plan(plan_view(job, plan))
    context.console.blank()
    confirmed = arguments.yes or context.prompts.confirm_plan(plan.summary.total_items)
    if not confirmed:
        context.console.message("plan.cancelled", style="warning")
        return 1
    try:
        context.transfers.confirm_plan(
            job,
            plan_id=plan.plan_id,
            revision=plan.revision,
            plan_hash=plan.plan_hash,
        )
        result = context.transfers.execute(
            job, destination_adapter, confirmed=True, progress=_progress(context)
        )
    except ConfirmationRequired as error:
        context.console.error(error.code)
        return 2
    context.console.blank()
    context.console.report(report_view(job, result["report"]))
    verification = result.get("verification") or {}
    context.console.verification(
        verification_views(
            {key: _result_from_mapping(value) for key, value in verification.items()}
        )
    )
    return 0 if result["report"].failed == 0 else 1


def command_resume(context: CliContext, arguments: Any) -> int:
    """Continue an interrupted job from durable item state."""

    job = context.transfers.jobs.get(arguments.job_id)
    if job is None:
        context.console.text(f"Job not found: {arguments.job_id}", style="error")
        return 2
    if job.is_finished:
        context.console.error("job_already_finished")
        return 1
    pending = context.transfers.recovery.pending_items(job.id)
    context.console.message("resume.pending", count=len(pending))
    if not pending:
        context.console.message("resume.nothing_pending", style="success")
        return 0
    destination = _require_connection(
        context, job.destination_platform, "destination"
    )
    if destination is None:
        return 2
    confirmed = arguments.yes or context.prompts.yes_no("transfer.resume_question")
    if not confirmed:
        context.console.message("transfer.resume_declined", style="warning")
        return 1
    result = context.transfers.resume(
        job,
        _adapter(context, destination, "destination"),
        confirmed=True,
        progress=_progress(context),
    )
    context.console.blank()
    context.console.report(report_view(job, result["report"]))
    return 0 if result["report"].failed == 0 else 1


def command_retry(context: CliContext, arguments: Any) -> int:
    """Create a follow-up job containing only the items worth retrying."""

    job = context.transfers.jobs.get(arguments.job_id)
    if job is None:
        context.console.text(f"Job not found: {arguments.job_id}", style="error")
        return 2
    retry_job = context.transfers.create_retry_job(job)
    if retry_job is None:
        context.console.message("resume.nothing_pending", style="success")
        return 0
    context.console.message(
        "resume.retry_created", count=retry_job.total_items, style="success"
    )
    context.console.message("job.job_line", job_id=retry_job.id, status=retry_job.status.value)
    return 0


def command_jobs(context: CliContext, arguments: Any) -> int:
    """List known transfer jobs and their durable status."""

    jobs = context.transfers.jobs.list_all()
    if not jobs:
        context.console.text("No jobs recorded yet.")
        return 0
    for job in jobs:
        context.console.message(
            "job.job_line",
            job_id=job.id,
            status=context.console.i18n.t(f"job.status.{job.status.value}"),
        )
    return 0


def command_export(context: CliContext, arguments: Any) -> int:
    """Export a source library snapshot to a JSON file (a portable backup)."""

    source = _require_connection(context, _platform(arguments.source), "source")
    if source is None:
        return 2
    snapshot = _adapter(context, source, "source").export_library(
        progress=_export_progress(context)
    )
    path = context.settings.reports / arguments.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snapshot.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if snapshot.is_partial:
        context.console.error(
            "partial_export", sections=", ".join(snapshot.incomplete_sections)
        )
    context.console.text(f"Snapshot written: {path}", style="success")
    return 0


def command_capabilities(context: CliContext, arguments: Any) -> int:
    """Print the declared capabilities of one platform.

    Platforms without an adapter are reported as such instead of pretending to
    support anything.
    """

    platform = _platform(arguments.platform)
    capabilities = context.registry.capabilities_for(platform)
    if capabilities is None:
        context.console.error(
            "platform_not_implemented", capability=f"platform:{platform.value}"
        )
        return 1
    context.console.text(json.dumps(capabilities.as_dict(), indent=2, sort_keys=True))
    return 0


# -- helpers ---------------------------------------------------------------


def _platform(value: str) -> Platform:
    """Resolve a CLI platform name, failing loudly on an unknown value."""

    return Platform(str(value).strip().casefold())


def _content(arguments: Any) -> tuple[ContentType, ...]:
    """Build the requested content tuple from parsed arguments."""

    names = [item.strip().casefold() for item in (arguments.content or "").split(",") if item.strip()]
    if not names:
        return (ContentType.LIKED_TRACKS,)
    selected: list[ContentType] = []
    for name in names:
        if name not in CONTENT_CHOICES:
            raise TransferConfigurationError("unsupported_content_type")
        selected.append(CONTENT_CHOICES[name])
    return tuple(selected)


def _settings_from_arguments(arguments: Any) -> TransferSettings:
    """Build transfer settings from CLI flags."""

    ordering = ORDERING_CHOICES.get(
        str(getattr(arguments, "order", "original") or "original").casefold(),
        OrderingMode.SOURCE_ORDER,
    )
    return TransferSettings(
        ordering=ordering,
        dry_run=bool(getattr(arguments, "dry_run", False)),
        preserve_visible_order=bool(getattr(arguments, "preserve_order", False)),
        allow_duplicates_in_playlists=not bool(getattr(arguments, "dedupe", False)),
    )


def _require_connection(context: CliContext, platform: Platform, role: str):
    """Return the connected account for a platform, or ``None`` with a message."""

    try:
        return context.connect(platform, role)
    except MusicTransferError as error:
        context.console.error(error.code)
        return None


def _adapter(context: CliContext, account, role: str):
    """Return a live adapter for a connected account."""

    return context.accounts.adapter_for(account, role)


def _export_progress(context: CliContext):
    """Return an export-progress sink bound to the console."""

    def report(section: str, current: int, total: int) -> None:
        context.progress.update(section, current, total, operation="backup")

    return report


def _progress(context: CliContext):
    """Return a transfer-progress sink bound to the console."""

    def report(progress: Any) -> None:
        context.progress.update(
            "tracks",
            progress.processed,
            progress.total,
            item=progress.current_item,
            errors=progress.failed,
            operation="transfer",
        )

    return report


def _result_from_mapping(value: dict[str, Any]):
    """Rebuild a :class:`VerificationResult` from its serialized form."""

    from ...core.domain import VerificationResult

    return VerificationResult(
        success=bool(value.get("success")),
        expected_count=int(value.get("expected_count", 0)),
        actual_count=int(value.get("actual_count", 0)),
        missing=list(value.get("missing") or []),
        unexpected=list(value.get("unexpected") or []),
        order_mismatches=list(value.get("order_mismatches") or []),
        warnings=list(value.get("warnings") or []),
    )


__all__ = [
    "CONTENT_CHOICES",
    "ORDERING_CHOICES",
    "command_accounts",
    "command_capabilities",
    "command_diagnostics",
    "command_export",
    "command_jobs",
    "command_plan",
    "command_resume",
    "command_retry",
    "command_transfer",
]

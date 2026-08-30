"""Serialization-friendly view models for interfaces.

Each ``*_view`` function maps domain objects onto a DTO.  Keeping the mapping
here means the CLI and a future Telegram bot render *identical* text from the
same numbers, and neither of them needs to understand domain internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...core.domain import (
    Account,
    TransferJob,
    TransferPlan,
    TransferReport,
    VerificationResult,
)
from ...core.enums import Platform


@dataclass(frozen=True, slots=True)
class AccountStatus:
    """What an interface needs to render one account row.

    Mirrors the screen sketched in the specification::

        TIDAL
        Connected
        roman

        Spotify
        Not connected
    """

    platform: Platform
    connected: bool
    display_name: str | None = None
    platform_account_id: str | None = None
    account_id: str | None = None
    #: True when the platform has no adapter yet (planned, not implemented).
    implemented: bool = True
    #: Set when the adapter exists but a required capability is missing.
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "platform": self.platform.value,
            "connected": self.connected,
            "display_name": self.display_name,
            "platform_account_id": self.platform_account_id,
            "account_id": self.account_id,
            "implemented": self.implemented,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class PlanView:
    """A pre-execution confirmation screen in data form."""

    job_id: str
    source_platform: Platform
    destination_platform: Platform
    source_label: str
    destination_label: str
    total_items: int = 0
    matched_items: int = 0
    ambiguous_items: int = 0
    not_found_items: int = 0
    already_exists_items: int = 0
    skipped_items: int = 0
    playlist_count: int = 0
    #: Per-content-type counts, already translated by the caller if needed.
    content_counts: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    #: True when the source snapshot was incomplete (never silently ignored).
    source_partial: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "job_id": self.job_id,
            "source_platform": self.source_platform.value,
            "destination_platform": self.destination_platform.value,
            "source_label": self.source_label,
            "destination_label": self.destination_label,
            "total_items": self.total_items,
            "matched_items": self.matched_items,
            "ambiguous_items": self.ambiguous_items,
            "not_found_items": self.not_found_items,
            "already_exists_items": self.already_exists_items,
            "skipped_items": self.skipped_items,
            "playlist_count": self.playlist_count,
            "content_counts": dict(self.content_counts),
            "warnings": list(self.warnings),
            "source_partial": self.source_partial,
        }


@dataclass(frozen=True, slots=True)
class ReportView:
    """A post-execution summary in data form."""

    job_id: str
    status: str
    total: int = 0
    transferred: int = 0
    already_existed: int = 0
    not_found: int = 0
    ambiguous: int = 0
    unavailable: int = 0
    skipped: int = 0
    failed: int = 0
    failures: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def succeeded(self) -> int:
        """Return items that ended up present on the destination."""

        return self.transferred + self.already_existed

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "job_id": self.job_id,
            "status": self.status,
            "total": self.total,
            "transferred": self.transferred,
            "already_existed": self.already_existed,
            "not_found": self.not_found,
            "ambiguous": self.ambiguous,
            "unavailable": self.unavailable,
            "skipped": self.skipped,
            "failed": self.failed,
            "failures": [dict(item) for item in self.failures],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class VerificationView:
    """A verification result in data form.

    ``matches`` means identifiers *and* order agree with the plan - not merely
    that the counts are equal.
    """

    scope: str
    matches: bool = False
    expected_count: int = 0
    actual_count: int = 0
    missing: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    order_mismatches: int = 0
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_result(cls, scope: str, result: VerificationResult) -> VerificationView:
        """Build a view from a domain result plus the scope it covers."""

        return cls(
            scope=scope,
            matches=result.success,
            expected_count=result.expected_count,
            actual_count=result.actual_count,
            missing=tuple(result.missing),
            unexpected=tuple(result.unexpected),
            order_mismatches=len(result.order_mismatches),
            warnings=tuple(result.warnings),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "scope": self.scope,
            "matches": self.matches,
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "missing": list(self.missing),
            "unexpected": list(self.unexpected),
            "order_mismatches": self.order_mismatches,
            "warnings": list(self.warnings),
        }


def account_statuses(
    accounts: dict[Platform, Account | None],
    *,
    implemented: dict[Platform, bool] | None = None,
) -> tuple[AccountStatus, ...]:
    """Build account rows for every platform the interface wants to show."""

    flags = implemented or {}
    statuses: list[AccountStatus] = []
    for platform, account in accounts.items():
        statuses.append(
            AccountStatus(
                platform=platform,
                connected=account is not None,
                display_name=account.display_name if account else None,
                platform_account_id=(
                    account.platform_account_id if account else None
                ),
                account_id=account.id if account else None,
                implemented=flags.get(platform, True),
            )
        )
    return tuple(statuses)


def plan_view(job: TransferJob, plan: TransferPlan) -> PlanView:
    """Map a plan and its job onto a confirmation-screen view model."""

    summary = plan.summary
    return PlanView(
        job_id=job.id,
        source_platform=job.source_platform,
        destination_platform=job.destination_platform,
        source_label=job.source_account_label or job.source_platform.value,
        destination_label=job.destination_account_label or job.destination_platform.value,
        total_items=summary.total_items,
        matched_items=summary.matched_items,
        ambiguous_items=summary.ambiguous_items,
        not_found_items=summary.not_found_items,
        already_exists_items=summary.already_exists_items,
        skipped_items=summary.skipped_items,
        playlist_count=summary.playlist_count,
        content_counts=dict(plan.metadata.get("content_counts") or {}),
        warnings=tuple(plan.warnings),
        source_partial=bool(plan.source_incomplete),
    )


def report_view(job: TransferJob, report: TransferReport) -> ReportView:
    """Map a report onto a summary view model."""

    return ReportView(
        job_id=job.id,
        status=job.status.value,
        total=report.total,
        transferred=report.transferred,
        already_existed=report.already_existed,
        not_found=report.not_found,
        ambiguous=report.ambiguous,
        unavailable=report.unavailable,
        skipped=report.skipped,
        failed=report.failed,
        failures=tuple(report.failures),
        warnings=tuple(report.warnings),
    )


def verification_views(results: dict[str, VerificationResult]) -> tuple[VerificationView, ...]:
    """Map every scope of a job verification onto view models."""

    return tuple(
        VerificationView.from_result(scope, result) for scope, result in results.items()
    )


__all__ = [
    "AccountStatus",
    "PlanView",
    "ReportView",
    "VerificationView",
    "account_statuses",
    "plan_view",
    "report_view",
    "verification_view",
]

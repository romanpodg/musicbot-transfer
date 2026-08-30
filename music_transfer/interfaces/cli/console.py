"""Localized console output and terminal progress rendering.

Migrated from the legacy ``ui/progress.py`` with two changes: messages resolve
through the new catalog layout, and the new view models (plan, report,
verification, accounts) have dedicated renderers so every interface shows the
same numbers from the same source.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from ...app.dto import AccountStatus, PlanView, ReportView, VerificationView
from ...locales import LocalizationManager


class Console:
    """Render all messages through the active localization catalog."""

    _colors = {
        "normal": "",
        "success": "\033[92m",
        "warning": "\033[93m",
        "error": "\033[91m",
        "heading": "\033[96m",
        "reset": "\033[0m",
    }

    def __init__(
        self,
        i18n: LocalizationManager,
        output: Callable[[str], None] | None = None,
        color: bool | None = None,
    ) -> None:
        self.i18n = i18n
        self._output = output or print
        self._uses_default_output = output is None
        self._color = sys.stdout.isatty() if color is None else color

    @property
    def is_terminal(self) -> bool:
        """Return whether this console can safely render a live progress bar."""

        return self._uses_default_output and sys.stdout.isatty()

    def message(self, key: str, *, style: str = "normal", **values: object) -> None:
        """Print a localized message with an optional semantic color."""

        self.text(self.i18n.t(key, **values), style=style)

    def error(self, code: str, **values: object) -> None:
        """Print a localized message for a stable core error code."""

        self.text(self.i18n.error(code, **values), style="error")

    def text(self, value: str, *, style: str = "normal") -> None:
        """Print caller-provided dynamic data, such as an OAuth URL."""

        if self._color and style != "normal":
            self._output(f"{self._colors[style]}{value}{self._colors['reset']}")
        else:
            self._output(value)

    def blank(self) -> None:
        """Print a deliberate visual separator."""

        self._output("")

    # -- structured renderers ---------------------------------------------

    def counts(self, counts: dict[str, int]) -> None:
        """Display library counts through localized category labels."""

        for category in (
            "tracks",
            "albums",
            "artists",
            "videos",
            "mixes",
            "folders",
            "playlists",
        ):
            if category in counts:
                self.message(
                    "summary.count_line",
                    label=self.i18n.t(f"category.{category}"),
                    count=counts[category],
                )

    def accounts(self, statuses: tuple[AccountStatus, ...]) -> None:
        """Render the connected/not-connected account screen."""

        self.message("accounts.heading", style="heading")
        for status in statuses:
            state = (
                "accounts.connected"
                if status.connected
                else "accounts.not_connected"
            )
            if not status.implemented:
                state = "accounts.not_implemented"
            self.text(f"{self.platform_name(status.platform)}")
            self.text(
                f"  {self.i18n.t(state)}",
                style="success" if status.connected else "normal",
            )
            name = status.display_name or status.platform_account_id
            if name:
                self.text(f"  {name}")

    def platform_name(self, platform: Any) -> str:
        """Return the localized display name of a platform."""

        return self.i18n.t(f"platform.{platform.value}")

    def plan(self, view: PlanView) -> None:
        """Render the pre-execution confirmation screen."""

        self.message("plan.heading", style="heading")
        self.message(
            "plan.route",
            source=f"{self.platform_name(view.source_platform)} ({view.source_label})",
            destination=(
                f"{self.platform_name(view.destination_platform)} "
                f"({view.destination_label})"
            ),
        )
        self.message("plan.total_items", count=view.total_items)
        self.message("plan.matched", count=view.matched_items)
        self.message("plan.already_exists", count=view.already_exists_items)
        self.message("plan.ambiguous", count=view.ambiguous_items)
        self.message("plan.unmatched", count=view.not_found_items)
        if view.playlist_count:
            self.message(
                "summary.count_line",
                label=self.i18n.t("category.playlists"),
                count=view.playlist_count,
            )
        for label, count in sorted(view.content_counts.items()):
            self.message("plan.content_line", label=label, count=count)
        for warning in view.warnings:
            self.message("plan.warning_line", style="warning", warning=warning)
        if view.source_partial:
            self.error("partial_export", sections=self.i18n.t("account.unknown"))

    def report(self, view: ReportView) -> None:
        """Render the post-execution summary."""

        self.message("report.heading", style="heading")
        self.message("job.status_line", status=self.i18n.t(f"job.status.{view.status}"))
        self.message("report.transferred", count=view.transferred)
        self.message("report.already_existed", count=view.already_existed)
        self.message("report.not_found", count=view.not_found)
        self.message("report.unavailable", count=view.unavailable)
        self.message("report.ambiguous", count=view.ambiguous)
        self.message("report.skipped", count=view.skipped)
        if view.failed:
            self.message("report.failed", count=view.failed, style="warning")

    def verification(self, views: tuple[VerificationView, ...]) -> None:
        """Render verification results, one block per scope."""

        if not views:
            return
        self.message("verification.heading", style="heading")
        for view in views:
            if view.matches:
                self.text(f"✅ {view.scope}", style="success")
                continue
            self.text(f"⚠️ {view.scope}", style="warning")
            if view.missing:
                self.message("verification.missing", count=len(view.missing))
            if view.unexpected:
                self.message("verification.unexpected", count=len(view.unexpected))
            if view.order_mismatches:
                self.message(
                    "verification.order_mismatches", count=view.order_mismatches
                )
            for warning in view.warnings:
                self.message("plan.warning_line", style="warning", warning=warning)

    def diagnostics(self, results: list[Any]) -> None:
        """Render display-safe local diagnostics supplied by the application."""

        self.message("diagnostics.heading", style="heading")
        for result in results:
            detail = f" {result.detail}" if result.detail else ""
            self.message(
                "diagnostics.line",
                label=self.i18n.t(f"diagnostics.label.{result.name}"),
                status=self.i18n.t(f"diagnostics.status.{result.status}"),
                detail=detail,
                style="success"
                if result.status in {"ok", "available", "found"}
                else "warning",
            )


class ProgressRenderer:
    """Use ``tqdm`` on an interactive terminal and readable fallback lines elsewhere."""

    def __init__(self, console: Console, width: int = 24) -> None:
        self._console = console
        self._width = width
        self._bar: Any | None = None
        self._category: str | None = None
        self._operation: str | None = None
        self._last_unknown_category: str | None = None

    def update(
        self,
        category: str,
        current: int,
        total: int,
        *,
        item: str | None = None,
        errors: int = 0,
        operation: str = "transfer",
    ) -> None:
        """Render an operation's current item, error count, and elapsed bar."""

        if total <= 0:
            self._render_unknown_total(category, current, operation)
            return
        if self._use_tqdm():
            self._render_tqdm(category, current, total, item, errors, operation)
            return
        self._render_fallback(category, current, total, item, errors, operation)

    def finish(self) -> None:
        """Close any live progress display before prompts or shutdown output."""

        if self._bar is not None:
            self._bar.close()
        self._bar = None
        self._category = None
        self._operation = None
        self._last_unknown_category = None

    def _use_tqdm(self) -> bool:
        if not self._console.is_terminal:
            return False
        try:
            import tqdm  # noqa: PLC0415 - optional terminal presentation dependency

            return callable(getattr(tqdm, "tqdm", None))
        except ImportError:
            return False

    def _render_tqdm(
        self,
        category: str,
        current: int,
        total: int,
        item: str | None,
        errors: int,
        operation: str,
    ) -> None:
        from tqdm import tqdm

        if self._bar is None or category != self._category or operation != self._operation:
            self.finish()
            self._bar = tqdm(
                total=total,
                desc=self._description(category, operation),
                unit=self._console.i18n.t("progress.item_unit"),
                dynamic_ncols=True,
                leave=True,
            )
            self._category = category
            self._operation = operation
        self._bar.total = total
        self._bar.n = min(max(current, 0), total)
        postfix = self._console.i18n.t(
            "progress.postfix",
            item=item or self._console.i18n.t("progress.not_available"),
            errors=errors,
        )
        self._bar.set_postfix_str(postfix, refresh=True)

    def _render_fallback(
        self,
        category: str,
        current: int,
        total: int,
        item: str | None,
        errors: int,
        operation: str,
    ) -> None:
        safe_current = min(max(current, 0), total)
        filled = round(self._width * safe_current / max(total, 1))
        bar = "█" * filled + "░" * (self._width - filled)
        self._console.message(
            "progress.line",
            label=self._description(category, operation),
            bar=bar,
            current=safe_current,
            total=total,
        )
        if item:
            self._console.message("progress.current", item=item)
        self._console.message("progress.errors", errors=errors)

    def _render_unknown_total(self, category: str, current: int, operation: str) -> None:
        if category != self._last_unknown_category or current == 0 or current % 100 == 0:
            self._console.message(
                "progress.scanning",
                label=self._description(category, operation),
                current=current,
            )
        self._last_unknown_category = category

    def _description(self, category: str, operation: str) -> str:
        return self._console.i18n.t(
            "progress.description",
            operation=self._console.i18n.t(f"progress.operation.{operation}"),
            label=self._console.i18n.t(f"category.{category}"),
        )


__all__ = ["Console", "ProgressRenderer"]

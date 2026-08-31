"""Command-line entrypoint.

The interactive TIDAL manager CLI (``tidal_manager/main.py``) remains available
for the original workflow.  This entrypoint drives the platform-independent
core and is the one to use for scripted runs, diagnostics, and development of
future platform adapters.

Commands::

    python -m music_transfer.interfaces.cli diagnostics
    python -m music_transfer.interfaces.cli accounts
    python -m music_transfer.interfaces.cli capabilities tidal
    python -m music_transfer.interfaces.cli plan --source tidal --destination tidal
    python -m music_transfer.interfaces.cli transfer --source tidal --destination tidal
    python -m music_transfer.interfaces.cli export --source tidal --output snapshot.json
    python -m music_transfer.interfaces.cli jobs
    python -m music_transfer.interfaces.cli resume <job-id>
    python -m music_transfer.interfaces.cli retry <job-id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ...core.enums import Platform
from ...core.errors import MusicTransferError
from . import commands
from .context import build_context


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the platform-independent CLI."""

    parser = argparse.ArgumentParser(
        prog="music-transfer",
        description="Platform-independent music library transfer (TIDAL first).",
    )
    parser.add_argument("--root", type=Path, default=None, help="Project/data root.")
    parser.add_argument("--language", choices=("en", "ru"), default=None)
    parser.add_argument("--log-level", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("diagnostics", help="Show environment and adapter checks.")
    subparsers.add_parser("accounts", help="Show connected music-service accounts.")

    capabilities = subparsers.add_parser("capabilities", help="Show a platform's capabilities.")
    capabilities.add_argument("platform", choices=[item.value for item in Platform])

    for name in ("plan", "transfer"):
        sub = subparsers.add_parser(
            name, help=f"{'Build and show' if name == 'plan' else 'Run'} a transfer plan."
        )
        _add_transfer_arguments(sub)

    export = subparsers.add_parser("export", help="Export a source library snapshot.")
    export.add_argument("--source", default=Platform.TIDAL.value)
    export.add_argument("--output", default="library-snapshot.json")

    subparsers.add_parser("jobs", help="List recorded transfer jobs.")

    resume = subparsers.add_parser("resume", help="Resume an interrupted job.")
    resume.add_argument("job_id")
    resume.add_argument("--yes", action="store_true")

    retry = subparsers.add_parser("retry", help="Create a retry job for failed items.")
    retry.add_argument("job_id")

    return parser


def _add_transfer_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the flags shared by the plan and transfer commands."""

    parser.add_argument("--source", default=Platform.TIDAL.value)
    parser.add_argument("--destination", default=Platform.TIDAL.value)
    parser.add_argument(
        "--content",
        default="tracks",
        help="Comma-separated: tracks,albums,artists,playlists,videos,mixes",
    )
    parser.add_argument(
        "--order",
        choices=sorted(commands.ORDERING_CHOICES),
        default="original",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan and simulate; write nothing.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument(
        "--dedupe", action="store_true", help="Drop repeated tracks inside a playlist."
    )
    parser.add_argument(
        "--preserve-order",
        action="store_true",
        help="Compensate for platforms that insert new items at the top.",
    )


def main(arguments: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the matching command."""

    _configure_unicode_output()
    parser = build_parser()
    parsed = parser.parse_args(arguments)
    root = parsed.root or _default_root()
    context = build_context(
        root,
        language=parsed.language,
        log_level=parsed.log_level,
    )
    handlers = {
        "diagnostics": lambda: commands.command_diagnostics(context),
        "accounts": lambda: commands.command_accounts(context),
        "capabilities": lambda: commands.command_capabilities(context, parsed),
        "plan": lambda: commands.command_plan(context, parsed),
        "transfer": lambda: commands.command_transfer(context, parsed),
        "export": lambda: commands.command_export(context, parsed),
        "jobs": lambda: commands.command_jobs(context, parsed),
        "resume": lambda: commands.command_resume(context, parsed),
        "retry": lambda: commands.command_retry(context, parsed),
    }
    try:
        return int(handlers[parsed.command]())
    except MusicTransferError as error:
        # The core carries stable codes; the interface renders them.
        context.console.error(error.code)
        context.logger.error(
            "event=command_failed command=%s code=%s", parsed.command, error.code
        )
        return 2
    except KeyboardInterrupt:
        context.console.message("app.interrupted", style="warning")
        return 130


def _default_root() -> Path:
    """Return the repository root (three levels above this module)."""

    return Path(__file__).resolve().parents[3]


def _configure_unicode_output() -> None:
    """Use UTF-8 safely when a Windows legacy console defaults to a code page."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue


if __name__ == "__main__":
    raise SystemExit(main())

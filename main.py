"""Entrypoint for the interactive TIDAL Library Manager CLI."""

from __future__ import annotations

import sys
from pathlib import Path

from core.diagnostics import DiagnosticsService
from core.state import ApplicationConfig, configure_logging, ensure_data_directories
from localization.manager import LocalizationManager
from ui.menu import ApplicationPaths, TidalManagerApplication
from ui.progress import Console


def main(arguments: list[str] | None = None) -> int:
    """Initialize application state and run the requested safe CLI mode."""

    _configure_unicode_output()
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    root = Path(__file__).resolve().parent
    ensure_data_directories(root)
    paths = ApplicationPaths(root=root)
    config = ApplicationConfig.load(paths.config)
    i18n = LocalizationManager(root / "localization", config.language or "en")
    i18n.validate_catalogs()
    logger = configure_logging(root / "data" / "logs" / "tidal_manager.log", config.log_level)
    console = Console(i18n)
    if arguments == ["--diagnostics"]:
        console.diagnostics(DiagnosticsService(root, logger).run())
        return 0
    if any(argument not in {"--dry-run"} for argument in arguments) or len(arguments) > 1:
        console.message("app.invalid_arguments", style="error")
        return 2
    TidalManagerApplication(paths, config, i18n, logger, dry_run="--dry-run" in arguments).run()
    return 0


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

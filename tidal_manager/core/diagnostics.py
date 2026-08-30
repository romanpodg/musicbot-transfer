"""Offline, credential-free application health diagnostics."""

from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from .state import ApplicationConfig


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    """One display-safe diagnostic result."""

    name: str
    status: str
    detail: str = ""


class DiagnosticsService:
    """Check local prerequisites without authenticating or changing TIDAL data."""

    def __init__(self, root: Path, logger: logging.Logger) -> None:
        self._root = root
        self._logger = logger

    def run(self) -> list[DiagnosticResult]:
        """Return deterministic local checks and log their sanitized status."""

        results = [
            self._python(),
            self._tidalapi(),
            self._config(),
            self._backup(),
            self._logs(),
            self._api_client(),
        ]
        self._logger.info(
            "event=diagnostics_completed failed=%d",
            sum(result.status == "error" for result in results),
        )
        return results

    @staticmethod
    def _python() -> DiagnosticResult:
        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        return DiagnosticResult("python", "ok" if sys.version_info >= (3, 12) else "error", version)

    @staticmethod
    def _tidalapi() -> DiagnosticResult:
        try:
            tidalapi = importlib.import_module("tidalapi")
            version = str(getattr(tidalapi, "__version__", "installed"))
            return DiagnosticResult("tidalapi", "ok", version)
        except Exception:
            return DiagnosticResult("tidalapi", "error")

    def _config(self) -> DiagnosticResult:
        try:
            ApplicationConfig.load(self._root / "config.json")
            return DiagnosticResult("config", "ok")
        except Exception as error:
            self._logger.error("event=diagnostics_config_failed error_type=%s", type(error).__name__)
            return DiagnosticResult("config", "error")

    def _backup(self) -> DiagnosticResult:
        path = self._root / "data" / "backups" / "tidal_backup.json"
        return DiagnosticResult("backup", "found" if path.is_file() else "missing")

    def _logs(self) -> DiagnosticResult:
        path = self._root / "data" / "logs" / "tidal_manager.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8"):
                pass
            return DiagnosticResult("logs", "ok")
        except OSError as error:
            self._logger.error("event=diagnostics_logs_failed error_type=%s", type(error).__name__)
            return DiagnosticResult("logs", "error")

    @staticmethod
    def _api_client() -> DiagnosticResult:
        try:
            tidalapi = importlib.import_module("tidalapi")
            if callable(getattr(tidalapi, "Session", None)):
                return DiagnosticResult("api", "available")
        except Exception:
            pass
        return DiagnosticResult("api", "unavailable")

"""Environment diagnostics for support and debugging.

The output is intentionally display-safe: no tokens, no account ids beyond what
a user already sees, and no filesystem secrets.  Interfaces render it verbatim.
"""

from __future__ import annotations

import logging
import platform as platform_module
import sys
from dataclasses import dataclass
from typing import Any

from ...config import Settings
from ...core.enums import Platform
from ...platforms.registry import PlatformRegistry, default_registry
from ..services.account_service import AccountService

_LOGGER = logging.getLogger("music_transfer.app.diagnostics")

#: Status values understood by the localized ``diagnostics.status.*`` keys.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FOUND = "found"
STATUS_MISSING = "missing"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One diagnostic line: a stable name, a status, and optional detail."""

    name: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {"name": self.name, "status": self.status, "detail": self.detail}


class DiagnosticsService:
    """Report the state the application actually depends on."""

    def __init__(
        self,
        settings: Settings,
        *,
        registry: PlatformRegistry | None = None,
        accounts: AccountService | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry or default_registry()
        self._accounts = accounts
        self._logger = logger or _LOGGER

    def run(self) -> list[CheckResult]:
        """Run every check, recording but never swallowing failures."""

        return [
            self._check_python(),
            self._check_dependencies(),
            self._check_config(),
            self._check_data_directories(),
            self._check_platforms(),
            self._check_credentials(),
            self._check_logs(),
        ]

    def as_dict(self) -> dict[str, Any]:
        """Serialize the whole report."""

        return {"checks": [check.as_dict() for check in self.run()]}

    # -- checks ------------------------------------------------------------

    def _check_python(self) -> CheckResult:
        """Report the interpreter version."""

        version = sys.version.split()[0]
        return CheckResult("python", STATUS_OK, f"{version} ({platform_module.system()})")

    def _check_dependencies(self) -> CheckResult:
        """Report whether the optional TIDAL SDK is importable."""

        try:
            import tidalapi  # noqa: PLC0415 - probing an optional dependency

            version = getattr(tidalapi, "__version__", "unknown")
        except ImportError:
            return CheckResult("tidalapi", STATUS_UNAVAILABLE, "not installed")
        return CheckResult("tidalapi", STATUS_AVAILABLE, str(version))

    def _check_config(self) -> CheckResult:
        """Report the resolved configuration, which never contains secrets."""

        try:
            values = self._settings.as_dict()
        except (OSError, ValueError) as error:
            self._logger.warning("event=diagnostics_config_failed error_type=%s", type(error).__name__)
            return CheckResult("config", STATUS_ERROR, type(error).__name__)
        return CheckResult(
            "config",
            STATUS_OK,
            f"language={values['language']} log_level={values['log_level']}",
        )

    def _check_data_directories(self) -> CheckResult:
        """Report whether the state directory is writable."""

        try:
            self._settings.state.mkdir(parents=True, exist_ok=True)
            probe = self._settings.state / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as error:
            self._logger.warning(
                "event=diagnostics_state_unwritable error_type=%s", type(error).__name__
            )
            return CheckResult("state", STATUS_ERROR, type(error).__name__)
        return CheckResult("state", STATUS_OK, str(self._settings.state))

    def _check_platforms(self) -> CheckResult:
        """Report which adapters are implemented versus merely planned."""

        implemented = [
            item.value for item in Platform if self._registry.capabilities_for(item) is not None
        ]
        planned = [item.value for item in Platform if item.value not in implemented]
        detail = f"implemented={','.join(implemented)} planned={','.join(planned)}"
        return CheckResult("adapters", STATUS_OK if implemented else STATUS_ERROR, detail)

    def _check_credentials(self) -> CheckResult:
        """Report whether the OS credential store is usable.

        Deliberately reports availability only - never a token, never a value.
        """

        try:
            from ...infrastructure.security.credentials import CredentialStore

            available = CredentialStore(self._logger).available()
        except Exception as error:  # noqa: BLE001 - diagnostics must never crash
            self._logger.warning(
                "event=diagnostics_keyring_failed error_type=%s", type(error).__name__
            )
            return CheckResult("keyring", STATUS_UNAVAILABLE, type(error).__name__)
        return CheckResult(
            "keyring", STATUS_AVAILABLE if available else STATUS_UNAVAILABLE
        )

    def _check_logs(self) -> CheckResult:
        """Report whether the log file exists."""

        path = self._settings.logs / "music_transfer.log"
        return CheckResult(
            "logs", STATUS_FOUND if path.is_file() else STATUS_MISSING, str(path)
        )


__all__ = ["CheckResult", "DiagnosticsService"]

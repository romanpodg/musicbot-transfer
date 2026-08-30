"""Credential storage.

Rules enforced here:

* OAuth tokens live in the OS keyring, never in project files, backups, reports,
  or logs;
* the application never asks for or stores a music-service password, so the
  future Telegram flow cannot become a password-collection flow;
* when no usable keyring backend exists the session is simply not persisted -
  it is never silently downgraded to a plaintext file.
"""

from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime
from typing import Any

#: Keyring service name.  The role (``source``/``destination``) is the username.
SERVICE_NAME = "music-transfer"

_REQUIRED_FIELDS = ("token_type", "access_token")


class CredentialStore:
    """Store OAuth credentials in the OS keyring."""

    _service_name = SERVICE_NAME

    def __init__(self, logger: logging.Logger, service_name: str | None = None) -> None:
        self._logger = logger
        if service_name:
            self._service_name = service_name

    def available(self) -> bool:
        """Return whether a usable credential backend exists.

        Reported by diagnostics so a user can tell "session not saved" apart
        from "login failed".  Never inspects or returns a credential.
        """

        return self._keyring_module() is not None

    def load(self, role: str) -> dict[str, Any] | None:
        """Load a role's OAuth credential, or ``None`` when unavailable.

        A malformed entry is deleted rather than returned: a half-valid token
        produces confusing authentication failures later.
        """

        keyring = self._keyring_module()
        if keyring is None:
            return None
        try:
            raw = keyring.get_password(self._service_name, role)
            if not raw:
                return None
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("credential_shape_invalid")
            if not all(isinstance(value.get(key), str) for key in _REQUIRED_FIELDS):
                raise ValueError("credential_fields_invalid")
            return {
                "token_type": str(value["token_type"]),
                "access_token": str(value["access_token"]),
                "refresh_token": _optional_string(value.get("refresh_token")),
                "expiry_time": _optional_string(value.get("expiry_time")),
            }
        except Exception as error:  # noqa: BLE001 - keyring backends vary widely
            # Only the exception type is logged; the payload may contain tokens.
            self._logger.warning(
                "event=credential_load_failed role=%s error_type=%s",
                role,
                type(error).__name__,
            )
            self.delete(role)
            return None

    def save(self, role: str, session: Any) -> bool:
        """Persist OAuth tokens when a secure backend is available."""

        keyring = self._keyring_module()
        if keyring is None:
            return False
        expiry = getattr(session, "expiry_time", None)
        payload = {
            "token_type": str(getattr(session, "token_type", "")),
            "access_token": str(getattr(session, "access_token", "")),
            "refresh_token": _optional_string(getattr(session, "refresh_token", None)),
            "expiry_time": expiry.isoformat() if hasattr(expiry, "isoformat") else None,
        }
        if not payload["token_type"] or not payload["access_token"]:
            self._logger.warning("event=credential_save_skipped role=%s", role)
            return False
        try:
            keyring.set_password(self._service_name, role, json.dumps(payload))
            return True
        except Exception as error:  # noqa: BLE001 - keyring backends vary widely
            self._logger.warning(
                "event=credential_save_failed role=%s error_type=%s",
                role,
                type(error).__name__,
            )
            return False

    def delete(self, role: str) -> None:
        """Remove a role's OAuth credential."""

        keyring = self._keyring_module()
        if keyring is None:
            return
        try:
            keyring.delete_password(self._service_name, role)
        except Exception:  # noqa: BLE001 - an absent entry is not an error here
            return

    def _keyring_module(self) -> Any | None:
        """Return ``keyring`` only when it has a usable non-fail backend."""

        try:
            keyring = importlib.import_module("keyring")
            backend = keyring.get_keyring()
            if "fail" in type(backend).__module__.lower():
                return None
            return keyring
        except Exception:  # noqa: BLE001 - a missing keyring is a supported state
            return None


def parse_expiry(value: str | None) -> datetime | None:
    """Deserialize a keyring expiry value for ``load_oauth_session``."""

    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def redacted_session_summary(session: Any) -> dict[str, Any]:
    """Return non-secret session facts safe for logs and diagnostics."""

    return {
        "token_type": str(getattr(session, "token_type", "")),
        "has_access_token": bool(getattr(session, "access_token", "")),
        "has_refresh_token": bool(getattr(session, "refresh_token", "")),
        "expiry_time": str(getattr(session, "expiry_time", "") or ""),
    }


def _optional_string(value: Any) -> str | None:
    """Convert a non-empty value to a string, or return ``None``."""

    return str(value) if value is not None and str(value) else None

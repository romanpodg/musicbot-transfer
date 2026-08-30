"""TIDAL OAuth authentication.

The flow is unchanged from the proven implementation: browser/device OAuth via
``tidalapi``, with sessions stored per role in the OS keyring.

Security rules:

* no music-service password is ever requested, stored, or logged, so a future
  Telegram flow cannot become a password-collection flow;
* tokens live only in the OS keyring; when no usable backend exists the session
  is not persisted and the user is told;
* only exception *types* and safe codes are logged; a credential payload is
  never formatted into a log record.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import Any

from ...core.errors import AuthenticationError
from ...infrastructure.http import RetryPolicy
from ...infrastructure.security import CredentialStore, parse_expiry
from .client import TidalLibraryClient

MessageCallback = Callable[..., None]


class AccountRole:
    """The two deliberately separate OAuth identities used by the application.

    Kept as string constants rather than an enum because the values are used as
    keyring usernames and persisted in legacy state files.
    """

    SOURCE = "source"
    DESTINATION = "destination"

    @classmethod
    def all(cls) -> tuple[str, ...]:
        """Return every supported role."""

        return (cls.SOURCE, cls.DESTINATION)


class TidalAuthenticator:
    """Establish independent ``tidalapi`` OAuth sessions for each account role."""

    def __init__(
        self,
        credentials: CredentialStore,
        logger: logging.Logger,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._credentials = credentials
        self._logger = logger
        self._retry_policy = retry_policy or RetryPolicy()

    def connect(
        self, role: str, emit: MessageCallback, retry_policy: RetryPolicy | None = None
    ) -> TidalLibraryClient:
        """Restore or start an OAuth flow for one account role.

        Args:
            role: One of :class:`AccountRole`'s values.
            emit: Callback used to show the OAuth URL.  The interface layer
                decides how to render it.
            retry_policy: Optional override for the session's retry policy.
        """

        session = self._new_session()
        if not self._restore_session(session, role):
            self._login_with_oauth(session, emit, role)
        try:
            if not self._retry(role).call("authentication_check", session.check_login):
                raise AuthenticationError("session_not_valid")
        except AuthenticationError:
            raise
        except Exception as error:  # noqa: BLE001 - normalized immediately below
            self._logger.warning(
                "event=authentication_validation_failed role=%s error_type=%s",
                role,
                type(error).__name__,
            )
            raise AuthenticationError("authentication_failed") from None
        if not self._credentials.save(role, session):
            emit("auth.session_not_saved")
        self._logger.info("event=authentication_completed role=%s", role)
        return TidalLibraryClient(
            session, self._logger, retry_policy or self._retry_policy
        )

    def forget(self, role: str) -> None:
        """Forget a previously saved OAuth session for one role."""

        self._credentials.delete(role)
        self._logger.info("event=authentication_forgotten role=%s", role)

    def _retry(self, role: str) -> Any:
        """Return a retry executor used for authentication calls."""

        from ...infrastructure.http import RetryExecutor

        del role
        return RetryExecutor(self._logger, self._retry_policy)

    def _new_session(self) -> Any:
        """Create and configure a new ``tidalapi`` session."""

        try:
            tidalapi = importlib.import_module("tidalapi")
            session = tidalapi.Session()
            from ...infrastructure.http import install_request_timeout

            install_request_timeout(session, self._retry_policy)
            return session
        except ImportError as error:
            raise AuthenticationError("tidalapi_missing") from error
        except Exception as error:  # noqa: BLE001 - normalized immediately below
            self._logger.error(
                "event=session_initialization_failed error_type=%s", type(error).__name__
            )
            raise AuthenticationError("session_initialization_failed") from None

    def _restore_session(self, session: Any, role: str) -> bool:
        """Restore a saved session, deleting it when it is no longer valid."""

        credential = self._credentials.load(role)
        if credential is None:
            return False
        expiry = parse_expiry(credential.get("expiry_time"))
        try:
            loaded = self._retry(role).call(
                "authentication_restore",
                lambda: session.load_oauth_session(
                    credential.get("token_type"),
                    credential.get("access_token"),
                    credential.get("refresh_token"),
                    expiry,
                ),
            )
            if loaded and self._retry(role).call(
                "authentication_restore_check", session.check_login
            ):
                self._logger.info("event=authentication_restored role=%s", role)
                return True
        except Exception as error:  # noqa: BLE001 - an expired session is expected
            self._logger.info(
                "event=authentication_restore_rejected role=%s error_type=%s",
                role,
                type(error).__name__,
            )
        self._credentials.delete(role)
        return False

    def _login_with_oauth(self, session: Any, emit: MessageCallback, role: str) -> None:
        """Run the device/browser OAuth flow."""

        try:
            login, future = self._retry(role).call("oauth_start", session.login_oauth)
            url = getattr(login, "verification_uri_complete", None)
            if not isinstance(url, str) or not url:
                raise AuthenticationError("oauth_url_missing")
            emit("auth.open_url", url=url)
            future.result()
            self._logger.info("event=oauth_login_completed role=%s", role)
        except AuthenticationError:
            raise
        except Exception as error:  # noqa: BLE001 - normalized immediately below
            self._logger.warning(
                "event=oauth_login_failed role=%s error_type=%s", role, type(error).__name__
            )
            raise AuthenticationError("oauth_login_failed") from None

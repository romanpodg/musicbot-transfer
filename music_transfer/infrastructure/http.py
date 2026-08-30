"""Bounded retry, timeout, and error-classification for HTTP-based platforms.

This is the migrated home of the proven ``tidal_manager.core.retry`` module.
The behaviour is unchanged; only the error vocabulary at the boundary differs,
because adapters now translate results into the core taxonomy.

Two deliberate rules:

* **Non-idempotent writes are not auto-retried.**  A timeout does not prove the
  write failed, so repeating a create/append could duplicate it.  Such calls
  still get a request timeout; their retry decision is left to the caller,
  which reconciles against destination state first.
* **Only transient failures are retried.**  404, 401/403, and other permanent
  4xx responses fail fast.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import requests

Result = TypeVar("Result")

#: Callback signature for sanitized retry notifications.
RetryCallback = Callable[["RetryEvent"], None]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bound every network wait and limit retry attempts."""

    timeout_seconds: float = 20.0
    max_attempts: int = 3
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 8.0


@dataclass(frozen=True, slots=True)
class RetryEvent:
    """A sanitized retry notification that an interface layer can localize."""

    operation: str
    attempt: int
    max_attempts: int
    reason: str
    delay_seconds: float


class ProviderRequestError(RuntimeError):
    """A normalized failure carrying no raw response body or credential data."""

    def __init__(self, reason: str, attempts: int, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts
        self.retryable = retryable


class RetryExecutor:
    """Run one provider operation with logged exponential-backoff retries."""

    def __init__(
        self,
        logger: logging.Logger,
        policy: RetryPolicy | None = None,
        on_retry: RetryCallback | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._logger = logger
        self._policy = policy or RetryPolicy()
        self._on_retry = on_retry
        self._sleep = sleep

    @property
    def policy(self) -> RetryPolicy:
        """Return the immutable policy used by this executor."""

        return self._policy

    def set_retry_callback(self, callback: RetryCallback | None) -> None:
        """Install a per-operation UI callback without storing UI state here."""

        self._on_retry = callback

    def call(
        self,
        operation: str,
        action: Callable[[], Result],
        *,
        retry_safe: bool = True,
    ) -> Result:
        """Return an action result or raise a sanitized, bounded failure.

        Args:
            operation: Short name used in log events.
            action: The call to perform.
            retry_safe: ``False`` for non-idempotent writes, which must not be
                repeated automatically because their remote outcome is unknown.

        Raises:
            ProviderRequestError: When the call ultimately fails.
        """

        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                return action()
            except KeyboardInterrupt:
                raise
            except Exception as error:  # noqa: BLE001 - classified immediately below
                reason, retryable, retry_after = classify_provider_error(error)
                can_retry = retry_safe and retryable and attempt < self._policy.max_attempts
                if not can_retry:
                    self._logger.error(
                        "event=api_request_failed operation=%s reason=%s attempts=%d retryable=%s",
                        operation,
                        reason,
                        attempt,
                        retryable,
                    )
                    raise ProviderRequestError(reason, attempt, retryable=retryable) from None
                delay = self._backoff_seconds(attempt, retry_after)
                self._logger.warning(
                    "event=api_request_retry operation=%s attempt=%d max_attempts=%d reason=%s delay_seconds=%.2f",
                    operation,
                    attempt,
                    self._policy.max_attempts,
                    reason,
                    delay,
                )
                if self._on_retry is not None:
                    self._on_retry(
                        RetryEvent(
                            operation=operation,
                            attempt=attempt,
                            max_attempts=self._policy.max_attempts,
                            reason=reason,
                            delay_seconds=delay,
                        )
                    )
                self._sleep(delay)
        raise AssertionError("retry loop must return or raise")

    def _backoff_seconds(self, attempt: int, retry_after: float | None) -> float:
        """Return the wait before the next attempt, honouring ``Retry-After``."""

        if retry_after is not None and retry_after >= 0:
            return min(retry_after, self._policy.max_backoff_seconds)
        delay = self._policy.initial_backoff_seconds * (2 ** (attempt - 1))
        return min(delay, self._policy.max_backoff_seconds)


def install_request_timeout(session: Any, policy: RetryPolicy) -> None:
    """Install a default timeout on every upstream ``requests`` call.

    ``tidalapi`` (and several other providers) expose a ``requests.Session`` but
    set no timeout, which can hang a transfer indefinitely.  Wrapping the
    instance's ``request`` method keeps that invariant even for calls made
    inside library-internal helpers.
    """

    request_session = getattr(session, "request_session", None)
    request = getattr(request_session, "request", None)
    if request_session is None or not callable(request):
        return
    if getattr(request_session, "_music_transfer_timeout_configured", False):
        return

    def request_with_timeout(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", policy.timeout_seconds)
        return request(*args, **kwargs)

    request_session.request = request_with_timeout
    request_session._music_transfer_timeout_configured = True  # type: ignore[attr-defined]


#: Compatibility alias for the name used by earlier releases.
configure_tidal_session = install_request_timeout


def classify_provider_error(error: Exception) -> tuple[str, bool, float | None]:
    """Map an upstream failure to a safe reason and retry classification.

    Returns:
        ``(reason, retryable, retry_after_seconds)``.  The reason is a stable
        code suitable for localization and logging; raw exception messages are
        never returned because they can contain URLs with tokens.
    """

    if isinstance(error, requests.Timeout):
        return "api_timeout", True, None
    if isinstance(error, requests.ConnectionError):
        return "network_error", True, None
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 401 or status_code == 403:
        return "authorization_error", False, None
    if status_code == 404:
        return "item_unavailable", False, None
    if status_code == 429:
        return "rate_limited", True, _retry_after(error)
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return "api_server_error", True, None
    name = type(error).__name__.lower()
    if "toomanyrequests" in name or "ratelimit" in name:
        return "rate_limited", True, _retry_after(error)
    if "unauthorized" in name or "forbidden" in name:
        return "authorization_error", False, None
    if "timeout" in name:
        return "api_timeout", True, None
    if "notfound" in name or "unavailable" in name:
        return "item_unavailable", False, None
    return "provider_error", False, None


def _retry_after(error: Exception) -> float | None:
    """Read a ``Retry-After`` value from an attribute or response header."""

    value = getattr(error, "retry_after", None)
    if isinstance(value, (int, float)):
        return float(value)
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    header = headers.get("Retry-After") if headers else None
    try:
        return float(header) if header is not None else None
    except (TypeError, ValueError):
        return None

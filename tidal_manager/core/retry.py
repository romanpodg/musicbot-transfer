"""Bounded retry and timeout support for TIDAL provider calls.

The upstream library exposes a ``requests.Session`` but does not set a request
timeout.  This module supplies the missing bounded network policy without
letting provider-specific exceptions leak into the rest of the application.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import requests


Result = TypeVar("Result")
RetryCallback = Callable[["RetryEvent"], None]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bound all network waits and retry only transient provider failures."""

    timeout_seconds: float = 20.0
    max_attempts: int = 3
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 8.0
    jitter_ratio: float = 0.15


@dataclass(frozen=True, slots=True)
class RetryEvent:
    """A sanitized retry notification that the UI can localize."""

    operation: str
    attempt: int
    max_attempts: int
    reason: str
    delay_seconds: float


class ProviderRequestError(RuntimeError):
    """A normalized failure with no raw response body or credential data."""

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
        """Return an action result or raise a sanitized bounded failure.

        Non-idempotent writes may have an unknown remote outcome after a
        timeout.  They still receive the configured request timeout, but are
        deliberately not automatically repeated; their durable caller state is
        reconciled before a later resume.
        """

        for attempt in range(1, self._policy.max_attempts + 1):
            started = time.monotonic()
            try:
                result = action()
                self._logger.debug(
                    "event=api_request_completed operation=%s duration_seconds=%.3f attempt=%d",
                    operation, time.monotonic() - started, attempt,
                )
                return result
            except KeyboardInterrupt:
                raise
            except Exception as error:
                reason, retryable, retry_after = classify_provider_error(error)
                can_retry = (
                    retry_safe
                    and retryable
                    and attempt < self._policy.max_attempts
                )
                if not can_retry:
                    self._logger.error(
                        "event=api_request_failed operation=%s reason=%s attempts=%d retryable=%s",
                        operation,
                        reason,
                        attempt,
                        retryable,
                    )
                    raise ProviderRequestError(
                        reason, attempt, retryable=retryable
                    ) from None
                delay = self._backoff_seconds(attempt, retry_after)
                self._logger.warning(
                    "event=api_request_retry operation=%s attempt=%d max_attempts=%d reason=%s delay_seconds=%.2f duration_seconds=%.3f",
                    operation,
                    attempt,
                    self._policy.max_attempts,
                    reason,
                    delay,
                    time.monotonic() - started,
                )
                if self._on_retry:
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
        if retry_after is not None and retry_after >= 0:
            # Retry-After is a provider instruction, not an advisory cap.
            return retry_after
        delay = self._policy.initial_backoff_seconds * (2 ** (attempt - 1))
        delay = min(delay, self._policy.max_backoff_seconds)
        if self._policy.jitter_ratio:
            delay *= random.uniform(1 - self._policy.jitter_ratio, 1 + self._policy.jitter_ratio)
        return delay


def configure_tidal_session(session: Any, policy: RetryPolicy) -> None:
    """Install a default timeout on every upstream ``requests`` call.

    ``tidalapi`` routes both reads and writes through ``request_session``.  An
    instance wrapper keeps that invariant even for API calls made inside its
    OAuth and paging helpers.
    """

    request_session = getattr(session, "request_session", None)
    request = getattr(request_session, "request", None)
    if request_session is None or not callable(request):
        return
    if getattr(request_session, "_tidal_manager_timeout_configured", False):
        return

    def request_with_timeout(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", policy.timeout_seconds)
        return request(*args, **kwargs)

    request_session.request = request_with_timeout
    request_session._tidal_manager_timeout_configured = True


def classify_provider_error(error: Exception) -> tuple[str, bool, float | None]:
    """Map an upstream failure to a safe reason and retry classification."""

    if isinstance(error, requests.Timeout):
        return "api_timeout", True, None
    if isinstance(error, requests.ConnectionError):
        return "network_error", True, None
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 404:
        return "item_unavailable", False, None
    if status_code == 429:
        return "rate_limited", True, _retry_after(error)
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return "api_server_error", True, None
    name = type(error).__name__.lower()
    if "toomanyrequests" in name or "ratelimit" in name:
        return "rate_limited", True, _retry_after(error)
    if "timeout" in name:
        return "api_timeout", True, None
    if "notfound" in name or "unavailable" in name:
        return "item_unavailable", False, None
    return "provider_error", False, None


def _retry_after(error: Exception) -> float | None:
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

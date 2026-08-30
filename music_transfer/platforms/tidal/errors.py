"""TIDAL-specific error translation.

The adapter boundary is where provider failures stop being provider failures
and become core errors.  Nothing above this module inspects a ``tidalapi``
exception class, and nothing below it needs to know the core taxonomy.

Error meanings are preserved from the proven implementation:

``item_unavailable`` -> :class:`UnavailableError`
    The item is not playable for this account/region.
``api_timeout`` / ``network_error`` / ``api_server_error`` / ``rate_limited``
    Transient failures.  On a *non-idempotent* write they additionally become
    :class:`AmbiguousOperationError`, because a timeout does not prove the
    write did not land.
"""

from __future__ import annotations

from typing import Any

from ...core.errors import (
    AmbiguousOperationError,
    AuthenticationError,
    AuthorizationError,
    MusicTransferError,
    NotFoundError,
    PermanentPlatformError,
    RateLimitError,
    TemporaryPlatformError,
    UnavailableError,
)

#: Reasons that mean "the remote outcome of a write is unknown".
AMBIGUOUS_REASONS = frozenset(
    {"api_timeout", "network_error", "api_server_error", "rate_limited"}
)

#: Provider reasons that mean the item cannot be used at all.
UNAVAILABLE_REASONS = frozenset({"item_unavailable", "not_found", "unavailable"})


class TidalClientError(PermanentPlatformError):
    """A normalized, non-secret TIDAL integration failure."""

    code = "tidal_client_error"

    def __init__(self, reason: str = "provider_error", attempts: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


class ItemUnavailableError(UnavailableError):
    """Raised when an item is no longer available to the account."""

    code = "item_unavailable"

    def __init__(self, reason: str = "item_unavailable", attempts: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


def translate_provider_error(
    error: Exception, *, operation_is_write: bool = False
) -> MusicTransferError:
    """Translate a normalized provider failure into the core taxonomy.

    Args:
        error: A :class:`ProviderRequestError` or a TIDAL client error.
        operation_is_write: Whether the failing call was a non-idempotent
            mutation, in which case a transient failure is reported as
            ambiguous so the caller reconciles instead of replaying.
    """

    reason = str(getattr(error, "reason", "") or "")
    attempts = int(getattr(error, "attempts", 0) or 0)
    if reason in UNAVAILABLE_REASONS:
        return ItemUnavailableError("item_unavailable", attempts)
    if reason == "authorization_error":
        return AuthorizationError("authorization_error")
    if reason == "rate_limited":
        retry_after = getattr(error, "retry_after_seconds", None)
        if operation_is_write:
            return AmbiguousOperationError("rate_limited_write_unconfirmed")
        return RateLimitError(retry_after_seconds=retry_after)
    if reason in AMBIGUOUS_REASONS:
        if operation_is_write:
            return AmbiguousOperationError(f"{reason}_write_unconfirmed")
        return TemporaryPlatformError(reason)
    if isinstance(error, TidalClientError):
        return TidalClientError(reason or "provider_error", attempts)
    return PermanentPlatformError(reason or "provider_error")


def is_ambiguous(error: Exception) -> bool:
    """Return whether an error means "unknown remote outcome"."""

    return isinstance(error, AmbiguousOperationError)


def looks_unavailable(error: Exception) -> bool:
    """Classify only normalized availability failures, never raw details.

    Used as a last resort for exceptions raised outside the retry wrapper.
    The exception *type name* is inspected because the message may contain a
    URL or identifier that must not be logged.
    """

    name = type(error).__name__.lower()
    return "notfound" in name or "unavailable" in name


def ensure_identifier(value: Any, code: str = "provider_id_missing") -> str:
    """Return an object's identifier or raise a normalized error."""

    identifier = getattr(value, "id", None)
    if identifier is None or not str(identifier):
        raise TidalClientError(code)
    return str(identifier)


__all__ = [
    "AMBIGUOUS_REASONS",
    "AuthenticationError",
    "ItemUnavailableError",
    "NotFoundError",
    "TidalClientError",
    "ensure_identifier",
    "is_ambiguous",
    "looks_unavailable",
    "translate_provider_error",
]

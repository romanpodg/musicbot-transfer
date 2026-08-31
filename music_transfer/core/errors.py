"""Semantic error taxonomy for the transfer core.

The taxonomy exists so callers can distinguish "try again later" from
"this will never work" from "we do not know what happened".  Platform adapters
translate provider-specific exceptions into these types at their boundary; the
core never inspects raw provider exception classes.

Every error carries a stable ``code``.  Interface layers (CLI, future Telegram)
map codes to localized messages.  Core code must not embed user-facing text.
"""

from __future__ import annotations


class MusicTransferError(Exception):
    """Base class for every error raised by the core.

    Attributes:
        code: A stable, snake_case identifier for localization and logging.
        retryable: Whether repeating the operation could plausibly succeed.
    """

    code = "music_transfer_error"
    retryable = False

    def __init__(self, code: str | None = None, message: str | None = None) -> None:
        self.code = code or type(self).code
        self.message = message or self.code
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.code


# --------------------------------------------------------------------------
# Authentication / authorization
# --------------------------------------------------------------------------


class AuthenticationError(MusicTransferError):
    """A session is missing, expired, or could not be established."""

    code = "authentication_error"
    retryable = False


class AuthorizationError(MusicTransferError):
    """The session is valid but lacks permission for the requested operation."""

    code = "authorization_error"
    retryable = False


# --------------------------------------------------------------------------
# Transport-level failures
# --------------------------------------------------------------------------


class RateLimitError(MusicTransferError):
    """The platform asked us to slow down (HTTP 429 or equivalent)."""

    code = "rate_limited"
    retryable = True

    def __init__(
        self,
        code: str | None = None,
        message: str | None = None,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(code, message)
        self.retry_after_seconds = retry_after_seconds


class TemporaryPlatformError(MusicTransferError):
    """A transient failure: timeout, network drop, or retryable 5xx."""

    code = "temporary_platform_error"
    retryable = True


class PermanentPlatformError(MusicTransferError):
    """The platform rejected the request in a way that will not change."""

    code = "permanent_platform_error"
    retryable = False


class NotFoundError(MusicTransferError):
    """The requested object does not exist in the destination catalog."""

    code = "not_found"
    retryable = False


class UnavailableError(MusicTransferError):
    """The object exists but is not playable in this region/subscription.

    Deliberately distinct from :class:`NotFoundError`: a catalog gap and a
    regional restriction require different user messaging and different retry
    behaviour.
    """

    code = "unavailable"
    retryable = False


class AmbiguousOperationError(MusicTransferError):
    """The operation's remote outcome is unknown.

    A timeout does **not** prove that a write did not happen.  When a mutation
    can only be applied once (playlist creation, single-item append) and the
    acknowledgement is missing, adapters raise this so callers reconcile
    against destination state instead of blindly replaying.
    """

    code = "ambiguous_operation"
    retryable = False


# --------------------------------------------------------------------------
# Capability / usage errors
# --------------------------------------------------------------------------


class UnsupportedCapabilityError(MusicTransferError):
    """The adapter does not support the requested operation.

    Raised instead of silently returning an empty or ``True`` result so that an
    unsupported feature is represented explicitly.
    """

    code = "unsupported_capability"
    retryable = False

    def __init__(
        self,
        code: str | None = None,
        message: str | None = None,
        *,
        capability: str | None = None,
    ) -> None:
        super().__init__(code, message)
        self.capability = capability


class TransferConfigurationError(MusicTransferError):
    """The requested transfer cannot be planned safely (e.g. same account)."""

    code = "transfer_configuration_error"
    retryable = False


class InvalidStateTransition(TransferConfigurationError):
    """A transfer job was asked to move to a state it cannot legally reach."""

    code = "invalid_state_transition"
    retryable = False

    def __init__(
        self,
        code: str | None = None,
        message: str | None = None,
        *,
        current: str | None = None,
        target: str | None = None,
    ) -> None:
        super().__init__(code, message)
        self.current = current
        self.target = target


class ConfirmationRequired(MusicTransferError):
    """A mutating or destructive operation was attempted without confirmation.

    Confirmation is an application/UI concern, but the service layer refuses to
    proceed without it so that a forgotten UI check cannot cause a write.
    """

    code = "confirmation_required"
    retryable = False


class PaginationError(MusicTransferError):
    """A paginated read stopped making progress or exceeded its safety bound."""

    code = "pagination_error"
    retryable = False


class PersistenceError(MusicTransferError):
    """A repository could not read or write its durable state."""

    code = "persistence_error"
    retryable = False


class InvalidPersistedStateError(PersistenceError):
    """Persisted state contains invalid, corrupted, or unrecognizable data."""

    code = "invalid_persisted_state"
    retryable = False


#: Errors that mean "re-running this item may succeed later".
RETRYABLE_ERROR_TYPES: tuple[type[MusicTransferError], ...] = (
    RateLimitError,
    TemporaryPlatformError,
)

#: Errors that mean "the remote outcome is unknown; inspect before retrying".
AMBIGUOUS_ERROR_TYPES: tuple[type[MusicTransferError], ...] = (
    AmbiguousOperationError,
)


class ItemFailureKind:
    """String constants describing why a transfer item failed.

    Kept as plain constants rather than an enum because these values are
    persisted in JSON state, where they have always appeared as strings.
    """

    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"
    AUTHENTICATION = "authentication"
    TEMPORARY = "temporary"
    PERMANENT = "permanent"


def classify_error(error: Exception) -> str:
    """Map any exception onto the coarse failure kind used by transfer items.

    Unknown exception types are treated as permanent failures so that a coding
    mistake surfaces as a visible failure rather than an endless retry loop.
    The original exception is never swallowed here; callers chain it.
    """

    if isinstance(error, AmbiguousOperationError):
        return ItemFailureKind.AMBIGUOUS
    if isinstance(error, UnavailableError):
        return ItemFailureKind.UNAVAILABLE
    if isinstance(error, NotFoundError):
        return ItemFailureKind.NOT_FOUND
    if isinstance(error, (AuthenticationError, AuthorizationError)):
        return ItemFailureKind.AUTHENTICATION
    if isinstance(error, RETRYABLE_ERROR_TYPES):
        return ItemFailureKind.TEMPORARY
    return ItemFailureKind.PERMANENT

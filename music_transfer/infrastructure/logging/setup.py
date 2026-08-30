"""Structured, redacted application logging.

Log lines are ``key=value`` pairs so they stay greppable without a log shipper::

    2026-08-30 01:00:00 INFO event=item_transferred job_id=job_ab12 entity=track source_id=123

Required context for platform operations: ``job_id``, ``operation``,
``platform``, ``entity_type``, ``source_id``, ``offset``, ``attempt``,
``result``, ``duration``.

Credentials must never be logged (Invariant K).  :class:`SecretRedactionFilter`
scrubs token-shaped values as a defence in depth, but the primary rule is that
no code path formats a secret into a message.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: The application logger name.  Use ``logging.getLogger(LOGGER_NAME)`` or a
#: child name such as ``music_transfer.executor``.
LOGGER_NAME = "music_transfer"

_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5


class SecretRedactionFilter(logging.Filter):
    """Remove token-shaped values from every log record.

    This is a safety net, not a licence to log secrets.  It also drops the
    record's formatting arguments after rendering, so a lazily formatted secret
    cannot survive in a handler.
    """

    _patterns = (
        re.compile(
            r"(?i)(access[_ -]?token|refresh[_ -]?token|authorization|password|api[_ -]?key|secret)"
            r"\s*[=:]\s*(?:bearer\s+)?[^\s,;]+"
        ),
        re.compile(r"(?i)bearer\s+[^\s,;]+"),
        re.compile(r"(?i)\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.?[A-Za-z0-9_\-]*"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        """Scrub the rendered message and discard untrusted format arguments."""

        message = record.getMessage()
        for pattern in self._patterns:
            message = pattern.sub("[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


class ContextAdapter(logging.LoggerAdapter):
    """Attach stable context (job id, platform, operation) to log events."""

    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        extra = dict(self.extra)
        if extra:
            context = " ".join(f"{key}={value}" for key, value in sorted(extra.items()))
            return f"{context} {msg}", kwargs
        return msg, kwargs


def configure_logging(
    path: Path | None = None, level: str = "INFO", *, console: bool = False
) -> logging.Logger:
    """Configure the application logger with rotation and redaction.

    Args:
        path: Log file location.  ``None`` disables file logging.
        level: A ``logging`` level name.
        console: Also write to stderr.  Off by default because the CLI owns the
            terminal.
    """

    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        file_handler.addFilter(SecretRedactionFilter())
        logger.addHandler(file_handler)
    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        stream_handler.addFilter(SecretRedactionFilter())
        logger.addHandler(stream_handler)
    logger.info("event=logging_initialized")
    return logger


def with_context(logger: logging.Logger, **context: object) -> ContextAdapter:
    """Return an adapter that prefixes every message with stable context."""

    return ContextAdapter(logger, {key: value for key, value in context.items() if value is not None})


def resolve_log_level(environment: dict[str, str] | None = None) -> str:
    """Read the configured log level from the environment."""

    source = os.environ if environment is None else environment
    return str(source.get("MUSIC_TRANSFER_LOG_LEVEL", "INFO")).upper()

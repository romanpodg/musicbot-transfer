"""Command-line interface.

Kept deliberately thin: every command is a translation from argv to an
application-service call.  The CLI exists for development, debugging, and
scripted runs, and it will keep working once Telegram arrives because both use
the same services.
"""

from .main import build_parser, main

__all__ = ["build_parser", "main"]

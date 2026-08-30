"""Allow ``python -m music_transfer`` to start the CLI.

The executable logic lives in ``interfaces.cli``; this module is only a stable
entry point so the invocation does not change when Telegram or a web interface
is added later.
"""

from __future__ import annotations

from .interfaces.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())

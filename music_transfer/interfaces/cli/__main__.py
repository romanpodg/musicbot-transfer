"""Allow ``python -m music_transfer.interfaces.cli``."""

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())

"""Centralized configuration.

Nothing in the codebase should hard-code an API URL, timeout, page size, retry
count, or feature flag.  Values come from environment variables (optionally a
``.env`` file) with safe defaults, and sensitive values are *only* read from the
environment - never written into ``config.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Application data subdirectories created on demand.
DATA_DIRECTORIES: tuple[str, ...] = ("backups", "reports", "logs", "state")


def _environment_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable."""

    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _environment_int(name: str, default: int) -> int:
    """Parse an integer environment variable, falling back on bad input."""

    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _environment_float(name: str, default: float) -> float:
    """Parse a float environment variable, falling back on bad input."""

    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class HttpSettings:
    """Network bounds shared by every platform adapter."""

    timeout_seconds: float = 20.0
    max_attempts: int = 3
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 8.0

    @classmethod
    def from_env(cls, environment: dict[str, str] | None = None) -> HttpSettings:
        """Build settings from the environment."""

        source = os.environ if environment is None else environment
        return cls(
            timeout_seconds=_environment_float("MUSIC_TRANSFER_HTTP_TIMEOUT", 20.0),
            max_attempts=max(1, _environment_int("MUSIC_TRANSFER_HTTP_MAX_ATTEMPTS", 3)),
            initial_backoff_seconds=_environment_float("MUSIC_TRANSFER_BACKOFF_INITIAL", 1.0),
            max_backoff_seconds=_environment_float("MUSIC_TRANSFER_BACKOFF_MAX", 8.0),
        )


@dataclass(frozen=True, slots=True)
class PaginationSettings:
    """Pagination bounds used by offset-paginated platforms."""

    page_size: int = 50
    playlist_page_size: int = 100
    max_pages: int = 10_000
    max_items: int = 1_000_000

    @classmethod
    def from_env(cls, environment: dict[str, str] | None = None) -> PaginationSettings:
        """Build settings from the environment."""

        source = os.environ if environment is None else environment
        return cls(
            page_size=max(1, _environment_int("MUSIC_TRANSFER_PAGE_SIZE", 50)),
            playlist_page_size=max(1, _environment_int("MUSIC_TRANSFER_PLAYLIST_PAGE_SIZE", 100)),
            max_pages=max(1, _environment_int("MUSIC_TRANSFER_MAX_PAGES", 10_000)),
            max_items=max(1, _environment_int("MUSIC_TRANSFER_MAX_ITEMS", 1_000_000)),
        )


@dataclass(frozen=True, slots=True)
class MatchingSettings:
    """Thresholds for the matching engine."""

    high_confidence: float = 0.88
    ambiguous_threshold: float = 0.62
    fuzzy_enabled: bool = True
    max_candidates: int = 5

    @classmethod
    def from_env(cls, environment: dict[str, str] | None = None) -> MatchingSettings:
        """Build settings from the environment."""

        source = os.environ if environment is None else environment
        return cls(
            high_confidence=_environment_float("MUSIC_TRANSFER_MATCH_HIGH", 0.88),
            ambiguous_threshold=_environment_float("MUSIC_TRANSFER_MATCH_AMBIGUOUS", 0.62),
            fuzzy_enabled=_environment_bool("MUSIC_TRANSFER_MATCH_FUZZY", True),
            max_candidates=max(1, _environment_int("MUSIC_TRANSFER_MATCH_CANDIDATES", 5)),
        )


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    """Execution bounds and safety switches."""

    max_item_attempts: int = 3
    checkpoint_every_item: bool = True
    verify_after_execution: bool = True

    @classmethod
    def from_env(cls, environment: dict[str, str] | None = None) -> ExecutionSettings:
        """Build settings from the environment."""

        source = os.environ if environment is None else environment
        return cls(
            max_item_attempts=max(1, _environment_int("MUSIC_TRANSFER_MAX_ITEM_ATTEMPTS", 3)),
            checkpoint_every_item=_environment_bool("MUSIC_TRANSFER_CHECKPOINT_EVERY_ITEM", True),
            verify_after_execution=_environment_bool("MUSIC_TRANSFER_VERIFY", True),
        )


@dataclass(frozen=True, slots=True)
class Settings:
    """The complete application configuration."""

    project_root: Path
    data_root: Path
    language: str | None = None
    log_level: str = "INFO"
    http: HttpSettings = field(default_factory=HttpSettings)
    pagination: PaginationSettings = field(default_factory=PaginationSettings)
    matching: MatchingSettings = field(default_factory=MatchingSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    #: Feature flags for capabilities that are not implemented yet.
    features: dict[str, bool] = field(default_factory=dict)

    @property
    def backups(self) -> Path:
        """Return the backup directory."""

        return self.data_root / "backups"

    @property
    def reports(self) -> Path:
        """Return the report directory."""

        return self.data_root / "reports"

    @property
    def logs(self) -> Path:
        """Return the log directory."""

        return self.data_root / "logs"

    @property
    def state(self) -> Path:
        """Return the durable state directory."""

        return self.data_root / "state"

    @classmethod
    def load(
        cls, project_root: Path, environment: dict[str, str] | None = None
    ) -> Settings:
        """Load configuration, merging the JSON file and the environment.

        The JSON file holds non-secret user preferences only.  Environment
        variables take precedence, so secrets and per-deployment overrides never
        need to be written to disk.
        """

        source = os.environ if environment is None else environment
        config_path = project_root / "config.json"
        file_values = _read_config_file(config_path)
        data_root = Path(source.get("MUSIC_TRANSFER_DATA_DIR", str(project_root / "data")))
        if not data_root.is_absolute():
            data_root = project_root / data_root
        return cls(
            project_root=project_root,
            data_root=data_root,
            language=source.get("MUSIC_TRANSFER_LANGUAGE") or file_values.get("language"),
            log_level=str(source.get("MUSIC_TRANSFER_LOG_LEVEL", file_values.get("log_level", "INFO"))).upper(),
            http=HttpSettings.from_env(source),
            pagination=PaginationSettings.from_env(source),
            matching=MatchingSettings.from_env(source),
            execution=ExecutionSettings.from_env(source),
            features={
                "telegram": _environment_bool("MUSIC_TRANSFER_FEATURE_TELEGRAM", False),
                "queue_workers": _environment_bool("MUSIC_TRANSFER_FEATURE_WORKERS", False),
                "postgres": _environment_bool("MUSIC_TRANSFER_FEATURE_POSTGRES", False),
            },
        )

    def save_language(self, language: str) -> None:
        """Persist the non-secret language preference."""

        path = self.project_root / "config.json"
        values = _read_config_file(path)
        values["language"] = language
        values.setdefault("log_level", self.log_level)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize the configuration for diagnostics.

        Deliberately includes no secret values: there are none, because secrets
        are only ever read from the environment or the OS keyring.
        """

        return {
            "project_root": str(self.project_root),
            "data_root": str(self.data_root),
            "language": self.language,
            "log_level": self.log_level,
            "http": asdict(self.http),
            "pagination": asdict(self.pagination),
            "matching": asdict(self.matching),
            "execution": asdict(self.execution),
            "features": dict(self.features),
        }


def _read_config_file(path: Path) -> dict[str, Any]:
    """Read the non-secret configuration file, tolerating its absence."""

    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def ensure_data_directories(settings: Settings) -> None:
    """Create the application data directory tree."""

    for name in DATA_DIRECTORIES:
        (settings.data_root / name).mkdir(parents=True, exist_ok=True)


def load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` lines from a ``.env`` file into the environment.

    Existing environment variables win, so a real deployment can override the
    file.  This small parser avoids adding a dependency; it intentionally does
    no shell interpolation, which keeps secrets with special characters safe.
    """

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")

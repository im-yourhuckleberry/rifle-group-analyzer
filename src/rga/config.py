"""Runtime configuration read from the environment (RGA_DATA_DIR, RGA_HOST, RGA_PORT, RGA_LOG_LEVEL).

The server only ever binds a loopback address; any other host is a `ConfigError`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "::1", "localhost")
LOG_LEVELS: tuple[str, ...] = ("critical", "error", "warning", "info", "debug", "trace")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_LOG_LEVEL = "info"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class ConfigError(ValueError):
    """Raised for an invalid or unsafe configuration value; the message names the variable."""


@dataclass(frozen=True)
class Settings:
    """Validated server settings. Construction rejects a non-loopback host or bad port/level."""

    data_dir: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = DEFAULT_LOG_LEVEL

    def __post_init__(self) -> None:
        if self.host not in LOOPBACK_HOSTS:
            raise ConfigError(
                f"RGA_HOST must be one of {', '.join(LOOPBACK_HOSTS)} (loopback only), got {self.host!r}"
            )
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"RGA_PORT must be an integer from 1 to 65535, got {self.port!r}")
        if self.log_level not in LOG_LEVELS:
            raise ConfigError(
                f"RGA_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {self.log_level!r}"
            )

    @property
    def db_path(self) -> Path:
        """SQLite database file inside the data directory."""
        return self.data_dir / "rga.sqlite3"

    @property
    def photos_dir(self) -> Path:
        """Directory holding original uploads, named `<sha256>.<ext>`."""
        return self.data_dir / "photos"

    @property
    def working_dir(self) -> Path:
        """Directory holding rectified working images, named `<photo_id>.png`."""
        return self.data_dir / "working"


def _read(environ: Mapping[str, str], name: str, default: str) -> str:
    value = environ.get(name)
    if value is None:
        return default
    if not value.strip():
        raise ConfigError(f"{name} is set but empty; unset it to use the default ({default!r})")
    return value.strip()


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build `Settings` from `environ` (default `os.environ`); raise `ConfigError` on bad input."""
    env = os.environ if environ is None else environ
    data_dir = Path(_read(env, "RGA_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser().resolve()
    host = _read(env, "RGA_HOST", DEFAULT_HOST)
    port_text = _read(env, "RGA_PORT", str(DEFAULT_PORT))
    try:
        port = int(port_text)
    except ValueError:
        raise ConfigError(f"RGA_PORT must be an integer, got {port_text!r}") from None
    log_level = _read(env, "RGA_LOG_LEVEL", DEFAULT_LOG_LEVEL).lower()
    return Settings(data_dir=data_dir, host=host, port=port, log_level=log_level)

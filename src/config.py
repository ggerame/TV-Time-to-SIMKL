"""Application configuration loaded from environment variables / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of the application's runtime configuration."""

    simkl_client_id: str
    db_path: str
    simkl_api_delay_ms: int
    simkl_api_timeout_ms: int
    host: str
    port: int


def load_config() -> Config:
    """Read configuration from the environment (populated from .env if present)."""
    return Config(
        simkl_client_id=os.getenv("SIMKL_CLIENT_ID", "").strip(),
        db_path=os.getenv("DB_PATH", "data/tvtime_simkl.sqlite3").strip(),
        simkl_api_delay_ms=_int_env("SIMKL_API_DELAY_MS", 110),
        simkl_api_timeout_ms=_int_env("SIMKL_API_TIMEOUT_MS", 20000),
        host=os.getenv("HOST", "127.0.0.1").strip(),
        port=_int_env("PORT", 8080),
    )


#: Process-wide configuration, loaded once at import time.
CONFIG = load_config()

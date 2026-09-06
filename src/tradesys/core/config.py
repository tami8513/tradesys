from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Process-wide settings. Contains no tenant data and no secrets."""

    clients_dir: Path
    data_dir: Path
    memory_backend: str = "local"          # "local" | "chroma"
    earnings_file: Path | None = None      # operator-maintained earnings calendar
    finnhub_api_key: str = ""              # optional vendor earnings feed
    allow_live_trading: bool = False       # second lock on top of the per-client flag
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, root: Path | None = None) -> "Settings":
        base = root or Path(os.getenv("TRADESYS_ROOT", Path.cwd()))
        return cls(
            clients_dir=Path(os.getenv("TRADESYS_CLIENTS_DIR", base / "clients")),
            data_dir=Path(os.getenv("TRADESYS_DATA_DIR", base / "var")),
            memory_backend=os.getenv("TRADESYS_MEMORY_BACKEND", "local"),
            earnings_file=Path(
                os.getenv("TRADESYS_EARNINGS_FILE", base / "earnings.yaml")
            ),
            finnhub_api_key=os.getenv("TRADESYS_FINNHUB_API_KEY", ""),
            allow_live_trading=_flag("TRADESYS_ALLOW_LIVE_TRADING", False),
            log_level=os.getenv("TRADESYS_LOG_LEVEL", "INFO"),
        )

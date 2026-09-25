from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(env_path: Path) -> None:
    """Load .env into the process environment, without overriding what is already set.

    Optional dependency: if python-dotenv is not installed, .env is silently
    skipped rather than raising — a real environment variable (Docker, systemd,
    Streamlit Cloud secrets) always works with or without this file.
    """
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_path, override=False)


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
    # Platform-level market data. When set, demo/paper tenants trade on live
    # prices while their balance stays in our own paper ledger.
    market_data_key_id: str = ""
    market_data_secret_key: str = ""
    market_data_feed: str = "iex"          # iex (free) | sip (paid, full tape)
    allow_live_trading: bool = False       # second lock on top of the per-client flag
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, root: Path | None = None) -> "Settings":
        base = root or Path(os.getenv("TRADESYS_ROOT", Path.cwd()))
        _load_dotenv(base / ".env")
        return cls(
            clients_dir=Path(os.getenv("TRADESYS_CLIENTS_DIR", base / "clients")),
            data_dir=Path(os.getenv("TRADESYS_DATA_DIR", base / "var")),
            memory_backend=os.getenv("TRADESYS_MEMORY_BACKEND", "local"),
            earnings_file=Path(
                os.getenv("TRADESYS_EARNINGS_FILE", base / "earnings.yaml")
            ),
            finnhub_api_key=os.getenv("TRADESYS_FINNHUB_API_KEY", ""),
            market_data_key_id=os.getenv("TRADESYS_MARKET_DATA__KEY_ID", ""),
            market_data_secret_key=os.getenv("TRADESYS_MARKET_DATA__SECRET_KEY", ""),
            market_data_feed=os.getenv("TRADESYS_MARKET_DATA__FEED", "iex"),
            allow_live_trading=_flag("TRADESYS_ALLOW_LIVE_TRADING", False),
            log_level=os.getenv("TRADESYS_LOG_LEVEL", "INFO"),
        )

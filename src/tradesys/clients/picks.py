"""Stocks the client picked themselves, for the robot to watch alongside the leaders.

The pick is the client's decision about their own account, so it widens what the
Scout looks at; it never widens risk. Every pick still goes through the Risk
Manager like any other idea: minimum price, liquidity cap, position cap, stops.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from tradesys.clients.profile import ClientProfile, WatchlistEntry
from tradesys.core.enums import SetupType
from tradesys.core.errors import ConfigError
from tradesys.core.fileio import write_json_atomic

PICKS_FILE = "my_stocks.json"
MAX_PICKS = 10
PICK_SETUPS = frozenset(SetupType)
_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def normalize(symbol: str) -> str:
    """Validate a typed ticker at the input boundary; raise with a reason in Hebrew."""
    ticker = symbol.strip().upper()
    if not _TICKER.match(ticker):
        raise ConfigError("סימול לא תקין: עד 5 אותיות באנגלית, למשל NVDA")
    return ticker


def read_picks(tenant_dir: Path) -> list[str]:
    path = Path(tenant_dir) / PICKS_FILE
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [normalize(s) for s in raw.get("symbols", [])][:MAX_PICKS]
    except (ValueError, OSError, ConfigError, AttributeError):
        return []


def write_picks(tenant_dir: Path, symbols: list[str]) -> None:
    unique = list(dict.fromkeys(normalize(s) for s in symbols))
    if len(unique) > MAX_PICKS:
        raise ConfigError(f"אפשר לבחור עד {MAX_PICKS} מניות")
    write_json_atomic(Path(tenant_dir) / PICKS_FILE, {"symbols": unique})


def apply_picks(profile: ClientProfile, picks: list[str]) -> ClientProfile:
    """Add the picks to the watchlist and pin them so the Scout always looks."""
    if not picks:
        return profile
    listed = {entry.symbol for entry in profile.watchlist}
    added = tuple(WatchlistEntry(symbol=s, setups=PICK_SETUPS) for s in picks if s not in listed)
    return replace(profile, watchlist=profile.watchlist + added, pinned_symbols=tuple(picks))

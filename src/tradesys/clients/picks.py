"""Stocks the client picked themselves, for the robot to watch alongside the leaders.

The pick is the client's decision about their own account, so it widens what the
Scout looks at; it never widens risk. Every pick still goes through the Risk
Manager like any other idea: minimum price, liquidity cap, position cap, stops.

Each pick can also be marked:
  * priority: evaluated before the robot's own ideas, so it gets a free slot first;
  * ask first: when the robot wants to buy it, it asks the client instead of
    buying, and the client approves or declines (see proposals.py);
  * a plan from the strategy lab: which entry setups to wait for and how wide
    the stop sits, fitted to this stock's last five years.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from tradesys.clients.profile import ClientProfile, WatchlistEntry
from tradesys.core.enums import SetupType
from tradesys.core.errors import ConfigError
from tradesys.core.fileio import write_json_atomic

PICKS_FILE = "my_stocks.json"
MAX_PICKS = 10
# Most a standing approval lets the robot spend on one pick without asking.
PREAPPROVAL_MAX = 20_000.0
PICK_SETUPS = frozenset(SetupType)
_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


@dataclass(frozen=True, slots=True)
class Pick:
    symbol: str
    priority: bool = False
    ask_first: bool = False
    # Standing approval for one trading day (ISO date, New York): on that day an
    # "ask first" pick is bought without asking, up to `max_amount` dollars.
    preapproved_on: str = ""
    max_amount: float | None = None
    setups: tuple[str, ...] = ()          # empty: every setup
    stop_atr: float | None = None         # None: the profile's stop distance


def approval_day(now: datetime) -> date:
    """The trading day a standing approval given at `now` applies to.

    Before the close it is today; after it, the next weekday. A holiday simply
    passes with nothing bought, and the approval lapses with it.
    """
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo("America/New_York"))
    day = local.date()
    if local.time() >= time(16, 0):
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def normalize(symbol: str) -> str:
    """Validate a typed ticker at the input boundary; raise with a reason in Hebrew."""
    ticker = symbol.strip().upper()
    if not _TICKER.match(ticker):
        raise ConfigError("סימול לא תקין: עד 5 אותיות באנגלית, למשל NVDA")
    return ticker


def _pick(raw) -> Pick:
    # Picks saved before the options existed are bare ticker strings.
    if isinstance(raw, str):
        return Pick(normalize(raw))
    return Pick(
        normalize(raw["symbol"]),
        priority=bool(raw.get("priority", False)),
        ask_first=bool(raw.get("ask_first", False)),
        preapproved_on=str(raw.get("preapproved_on", "")),
        max_amount=float(raw["max_amount"]) if raw.get("max_amount") else None,
        setups=tuple(SetupType(s).value for s in raw.get("setups", ())),
        stop_atr=float(raw["stop_atr"]) if raw.get("stop_atr") else None,
    )


def read_picks(tenant_dir: Path) -> list[Pick]:
    path = Path(tenant_dir) / PICKS_FILE
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [_pick(item) for item in raw.get("symbols", [])][:MAX_PICKS]
    except (ValueError, OSError, ConfigError, AttributeError, KeyError, TypeError):
        return []


def write_picks(tenant_dir: Path, picks: list[Pick]) -> None:
    unique: dict[str, Pick] = {}
    for pick in picks:
        unique.setdefault(normalize(pick.symbol), replace(pick, symbol=normalize(pick.symbol)))
    if len(unique) > MAX_PICKS:
        raise ConfigError(f"אפשר לבחור עד {MAX_PICKS} מניות")
    write_json_atomic(
        Path(tenant_dir) / PICKS_FILE, {"symbols": [asdict(p) for p in unique.values()]}
    )


def apply_picks(profile: ClientProfile, picks: list[Pick]) -> ClientProfile:
    """Add the picks to the watchlist and pin them so the Scout always looks."""
    if not picks:
        return profile
    planned = {p.symbol: frozenset(SetupType(s) for s in p.setups) for p in picks if p.setups}
    watchlist = tuple(
        replace(entry, setups=planned[entry.symbol]) if entry.symbol in planned else entry
        for entry in profile.watchlist
    )
    listed = {entry.symbol for entry in watchlist}
    added = tuple(
        WatchlistEntry(symbol=p.symbol, setups=planned.get(p.symbol, PICK_SETUPS))
        for p in picks if p.symbol not in listed
    )
    return replace(
        profile,
        watchlist=watchlist + added,
        pick_stops=tuple((p.symbol, p.stop_atr) for p in picks if p.stop_atr),
        pinned_symbols=tuple(p.symbol for p in picks),
        priority_symbols=tuple(p.symbol for p in picks if p.priority),
        approval_symbols=tuple(p.symbol for p in picks if p.ask_first),
        preapprovals=tuple(
            (p.symbol, p.preapproved_on, p.max_amount) for p in picks if p.preapproved_on
        ),
    )

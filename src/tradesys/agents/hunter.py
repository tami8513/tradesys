from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from tradesys.agents.base import Agent
from tradesys.clients.profile import ClientProfile
from tradesys.core.clock import Clock, trading_day
from tradesys.core.fileio import write_json_atomic
from tradesys.core.tenancy import TenantContext
from tradesys.data.indicators import closes, sma
from tradesys.data.market_data import MarketDataProvider

MIN_DOLLAR_VOLUME = 50_000_000.0
HISTORY_BARS = 220
LEADERS_FILE = "leaders.json"


@dataclass(frozen=True, slots=True)
class Leader:
    symbol: str
    score: float          # blended 3- and 6-month return, in percent
    return_3m: float
    return_6m: float


class LeaderHunter(Agent):
    """Picks the market's current leaders out of the client's approved universe.

    "Hot" here means sustained strength, not today's spike: a stock that jumped
    this morning tends to give it back, while one that led for months tends to
    keep leading. A leader must trade above a rising 50/200-day average stack,
    turn over enough dollars to enter and exit cleanly, and rank by its blended
    3- and 6-month return. The list is recomputed once a week and only from bars
    up to today, so a replay can never rank on the future.
    """

    name = "hunter"

    def __init__(
        self,
        tenant: TenantContext,
        profile: ClientProfile,
        market_data: MarketDataProvider,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(tenant, profile, clock)
        self._market_data = market_data
        self._week: tuple[int, int] | None = None
        self._leaders: list[Leader] = []

    def leaders(self) -> list[Leader]:
        week = trading_day(self.clock).isocalendar()[:2]
        if week != self._week:
            self._leaders = self._saved(week) or self._rank_and_save(week)
            self._week = week
        return self._leaders

    # The scheduler builds a fresh session every cycle, so an in-memory list
    # would be re-ranked across the whole universe every 15 minutes: a minute of
    # requests each time, and a risk of the data vendor's rate limit.
    def _path(self) -> Path:
        return self.tenant.data_dir / LEADERS_FILE

    def _saved(self, week: tuple[int, int]) -> list[Leader] | None:
        try:
            raw = json.loads(self._path().read_text(encoding="utf-8"))
            if tuple(raw["week"]) != week or raw["count"] != self.profile.leaders_count:
                return None
            return [Leader(**item) for item in raw["leaders"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _rank_and_save(self, week: tuple[int, int]) -> list[Leader]:
        leaders = self.rank(self.profile.symbols)[: self.profile.leaders_count]
        write_json_atomic(
            self._path(),
            {
                "week": list(week),
                "count": self.profile.leaders_count,
                "leaders": [asdict(leader) for leader in leaders],
            },
        )
        return leaders

    def rank(self, symbols: Sequence[str]) -> list[Leader]:
        ranked: list[Leader] = []
        for symbol in symbols:
            try:
                bars = self._market_data.history(symbol, timeframe="1Day", limit=HISTORY_BARS)
            except Exception:  # one missing symbol must not stop the hunt
                continue
            if len(bars) < 200:
                continue
            values = closes(bars)
            sma50, sma200 = sma(values, 50), sma(values, 200)
            last = values[-1]
            dollar_volume = sum(b.close * b.volume for b in bars[-20:]) / 20
            if not (last > sma50 > sma200 and dollar_volume >= MIN_DOLLAR_VOLUME):
                continue
            return_3m = (last / values[-63] - 1) * 100
            return_6m = (last / values[-126] - 1) * 100
            ranked.append(Leader(symbol, (return_3m + return_6m) / 2, return_3m, return_6m))
        return sorted(ranked, key=lambda leader: leader.score, reverse=True)

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from conftest import make_profile

from tradesys.agents.hunter import LeaderHunter
from tradesys.backtest import ReplayMarketData
from tradesys.clients.profile import WatchlistEntry
from tradesys.core.clock import FrozenClock
from tradesys.core.enums import SetupType
from tradesys.core.models import Bar
from tradesys.core.tenancy import TenantContext

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def trend(daily: float, volume: float = 1_000_000) -> list[Bar]:
    return [
        Bar(START + timedelta(days=i), 100 * daily**i, 100 * daily**i * 1.01,
            100 * daily**i * 0.99, 100 * daily**i, volume)
        for i in range(260)
    ]


def hunter(tmp_path, bars, count=2):
    symbols = tuple(bars)
    profile = replace(
        make_profile(),
        watchlist=tuple(WatchlistEntry(s, frozenset({SetupType.PULLBACK})) for s in symbols),
        leaders_count=count,
    )
    market = ReplayMarketData(bars)
    market.set_day((START + timedelta(days=259)).date())
    clock = FrozenClock(START + timedelta(days=259))
    return LeaderHunter(TenantContext.create(profile.client_id, tmp_path), profile, market, clock)


def test_strongest_uptrends_lead_and_downtrends_never_qualify(tmp_path):
    bars = {"FAST": trend(1.004), "SLOW": trend(1.001), "FALL": trend(0.998), "MID": trend(1.002)}
    leaders = hunter(tmp_path, bars).leaders()
    assert [leader.symbol for leader in leaders] == ["FAST", "MID"]


def test_thinly_traded_names_are_skipped(tmp_path):
    bars = {"THIN": trend(1.004, volume=1_000), "LIQUID": trend(1.002)}
    leaders = hunter(tmp_path, bars).leaders()
    assert [leader.symbol for leader in leaders] == ["LIQUID"]


def test_ranking_never_uses_bars_after_today(tmp_path):
    bars = {"LATE": trend(1.0) + [Bar(START + timedelta(days=260 + i), 500, 505, 495, 500, 1e6)
                                   for i in range(5)]}
    leaders = hunter(tmp_path, bars, count=1).leaders()
    assert leaders == []      # flat until today; the future spike is invisible

"""The talent scout's daily round, run by the scheduler while the market is closed.

One report a day serves every client: the scout reads the market, not anyone's
account. It is written for the coming trading day, so a client opening the
portal before the bell sees what the scout found overnight.
"""

from __future__ import annotations

import logging
from datetime import date

from tradesys.agents.talent_scout import ScoutReport, TalentScout, report_path, save_reports
from tradesys.clients.universes import UNIVERSES
from tradesys.core.config import Settings
from tradesys.data.asset_search import AssetDirectory
from tradesys.data.indicators import closes
from tradesys.data.market_data import AlpacaMarketData

log = logging.getLogger("tradesys.scouting")

BENCHMARK = "SPY"


def scout_due(settings: Settings, day: date) -> bool:
    return bool(settings.market_data_key_id) and not report_path(settings.data_dir, day).exists()


def scout_market(settings: Settings, day: date) -> list[ScoutReport]:
    from tradesys.data.scouting_feeds import ScoutingFeeds, alpaca_assets

    key, secret = settings.market_data_key_id, settings.market_data_secret_key
    market = AlpacaMarketData(key, secret)
    directory = AssetDirectory(settings.data_dir / "_platform", alpaca_assets(key, secret))
    feeds = ScoutingFeeds(key, secret, settings.finnhub_api_key)

    spy = closes(market.history(BENCHMARK, timeframe="1Day", limit=130))
    benchmark_6m = (spy[-1] / spy[-126] - 1) * 100

    scout = TalentScout(
        market,
        names=directory.name,
        movers=feeds.movers,
        news=feeds.news,
        analysts=feeds.analysts,
        growth=feeds.growth,
        earnings_in_days=feeds.earnings_in_days,
    )
    reports = scout.shortlist(UNIVERSES["leaders"], benchmark_6m)
    save_reports(settings.data_dir, day, reports)
    log.info("talent scout: %d names on the shortlist for %s", len(reports), day)
    return reports

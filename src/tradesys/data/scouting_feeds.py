"""The outside world the talent scout reads: market screeners, news, analysts.

Every call answers "nothing" on failure rather than raising, so one vendor being
down thins a report instead of cancelling it. Responses are validated here, at
the boundary, into plain values.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import httpx

from tradesys.data.earnings import FinnhubEarningsCalendar

DATA_URL = "https://data.alpaca.markets"
ASSETS_URL = "https://paper-api.alpaca.markets/v2/assets"
FINNHUB_URL = "https://finnhub.io/api/v1"
NEWS_DAYS = 7
NEWS_PAGES = 5
# Finnhub's free plan allows 60 calls a minute.
FINNHUB_PAUSE_SECONDS = 1.1
# FINRA publishes short interest twice a month, about a week after each count.
FINRA_SHORT_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
SHORT_LOOKBACK_DAYS = 45
FINRA_BATCH = 100


def alpaca_assets(key_id: str, secret_key: str) -> Callable[[], list[dict]]:
    """A fetcher for the broker's list of tradable US listings."""

    def fetch() -> list[dict]:
        response = httpx.get(
            ASSETS_URL,
            params={"status": "active", "asset_class": "us_equity"},
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    return fetch


class ScoutingFeeds:
    def __init__(
        self,
        key_id: str,
        secret_key: str,
        finnhub_key: str = "",
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._finnhub_key = finnhub_key
        self._sleep = sleep
        self._earnings = FinnhubEarningsCalendar(finnhub_key) if finnhub_key else None

    def _alpaca(self, path: str, params: dict) -> dict:
        response = httpx.get(f"{DATA_URL}{path}", headers=self._headers, params=params, timeout=20)
        response.raise_for_status()
        return response.json()

    def _finnhub(self, path: str, symbol: str, **params):
        self._sleep(FINNHUB_PAUSE_SECONDS)
        response = httpx.get(
            f"{FINNHUB_URL}{path}",
            params={"symbol": symbol, "token": self._finnhub_key, **params},
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def movers(self) -> list[str]:
        """Today's most-traded names and biggest gainers."""
        symbols: list[str] = []
        try:
            active = self._alpaca("/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": 50})
            symbols += [str(item["symbol"]) for item in active.get("most_actives", [])]
            moved = self._alpaca("/v1beta1/screener/stocks/movers", {"top": 50})
            symbols += [str(item["symbol"]) for item in moved.get("gainers", [])]
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            pass
        return symbols

    def news(self, symbols: list[str]) -> list[dict]:
        """The last week's stories on these names, newest first."""
        start = (datetime.now(timezone.utc) - timedelta(days=NEWS_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params: dict = {"symbols": ",".join(symbols), "start": start, "limit": 50}
        stories: list[dict] = []
        try:
            for _ in range(NEWS_PAGES):
                page = self._alpaca("/v1beta1/news", params)
                stories += [
                    {
                        "headline": str(item.get("headline", "")),
                        "summary": str(item.get("summary", "")),
                        "symbols": [str(s) for s in item.get("symbols", [])],
                    }
                    for item in page.get("news", [])
                ]
                if not page.get("next_page_token"):
                    break
                params["page_token"] = page["next_page_token"]
        except (httpx.HTTPError, TypeError, ValueError):
            pass
        return stories

    def analysts(self, symbol: str) -> dict | None:
        """The latest monthly count of analyst ratings."""
        if not self._finnhub_key:
            return None
        try:
            rows = self._finnhub("/stock/recommendation", symbol)
            latest = rows[0] if isinstance(rows, list) and rows else None
            if not isinstance(latest, dict):
                return None
            keys = ("strongBuy", "buy", "hold", "sell", "strongSell")
            return {k: int(latest.get(k) or 0) for k in keys}
        except (httpx.HTTPError, TypeError, ValueError):
            return None

    def growth(self, symbol: str) -> dict | None:
        """Year-over-year revenue and earnings-per-share growth, in percent."""
        if not self._finnhub_key:
            return None
        try:
            metric = self._finnhub("/stock/metric", symbol, metric="all").get("metric") or {}
            keys = ("revenueGrowthTTMYoy", "epsGrowthTTMYoy")
            found = {k: float(metric[k]) for k in keys if isinstance(metric.get(k), (int, float))}
            return found or None
        except (httpx.HTTPError, AttributeError, TypeError, ValueError):
            return None

    def earnings_in_days(self, symbol: str) -> int | None:
        if self._earnings is None:
            return None
        self._sleep(FINNHUB_PAUSE_SECONDS)
        today = date.today()
        upcoming = self._earnings.lookup(symbol, today).next_date
        return (upcoming - today).days if upcoming else None

    def short_interest(self, symbols: list[str]) -> dict[str, dict]:
        """The latest short-interest count per symbol, from FINRA's public data."""
        start = (date.today() - timedelta(days=SHORT_LOOKBACK_DAYS)).isoformat()
        latest: dict[str, dict] = {}
        try:
            for i in range(0, len(symbols), FINRA_BATCH):
                response = httpx.post(
                    FINRA_SHORT_URL,
                    json={
                        "limit": 5000,
                        "fields": ["symbolCode", "settlementDate", "daysToCoverQuantity",
                                   "changePercent"],
                        "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": start,
                                              "endDate": date.today().isoformat()}],
                        "domainFilters": [{"fieldName": "symbolCode",
                                           "values": symbols[i:i + FINRA_BATCH]}],
                    },
                    headers={"Accept": "application/json"},
                    timeout=30,
                )
                response.raise_for_status()
                for row in response.json() or []:
                    symbol, when = str(row["symbolCode"]), str(row["settlementDate"])
                    if symbol in latest and latest[symbol]["as_of"] >= when:
                        continue
                    latest[symbol] = {
                        "as_of": when,
                        "days_to_cover": float(row.get("daysToCoverQuantity") or 0.0),
                        "change_pct": float(row.get("changePercent") or 0.0),
                    }
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            pass
        return latest

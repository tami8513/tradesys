from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Mapping, Protocol, Sequence

from tradesys.core.models import Bar


class MarketDataProvider(Protocol):
    def history(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]: ...

    def last_price(self, symbol: str) -> float: ...


class SyntheticMarketData:
    """Deterministic synthetic bars for development, tests and dry runs.

    It is *not* a simulator of real market behaviour; it exists so the full agent
    loop can be exercised end-to-end without a data vendor or network access.
    """

    _CANONICAL_BARS = 600

    def __init__(self, seed: int = 7, base_prices: Mapping[str, float] | None = None) -> None:
        self._seed = seed
        self._base = dict(base_prices or {})
        self._cache: dict[str, list[Bar]] = {}

    def _series(self, symbol: str) -> list[Bar]:
        """One canonical series per symbol, generated once and reused.

        `history` and `last_price` must agree: they slice the same series. Generating
        a fresh series per call length would make the live price unrelated to the last
        bar close, and every position would stop out against a phantom quote.
        """
        key = symbol.upper()
        if key in self._cache:
            return self._cache[key]

        rng = random.Random(f"{self._seed}:{key}")
        # Seeded RNG, not hash(): str hashing is salted per process, so hash() would
        # give a symbol a different base price on every run.
        price = self._base.get(key, round(rng.uniform(8.0, 48.0), 2))
        start = datetime.now(timezone.utc) - timedelta(days=self._CANONICAL_BARS)
        bars: list[Bar] = []
        for index in range(self._CANONICAL_BARS):
            drift = rng.uniform(-0.03, 0.032)
            open_ = price
            close = max(0.5, price * (1 + drift))
            high = max(open_, close) * (1 + abs(rng.uniform(0, 0.015)))
            low = min(open_, close) * (1 - abs(rng.uniform(0, 0.015)))
            bars.append(
                Bar(
                    ts=start + timedelta(days=index),
                    open=round(open_, 4),
                    high=round(high, 4),
                    low=round(low, 4),
                    close=round(close, 4),
                    volume=float(rng.randint(200_000, 4_000_000)),
                )
            )
            price = close

        self._cache[key] = bars
        return bars

    def history(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        del timeframe  # a single synthetic timeframe is enough for dry runs
        return self._series(symbol)[-limit:]

    def last_price(self, symbol: str) -> float:
        return self._series(symbol)[-1].close


class AlpacaMarketData:
    """Read-only market data via the Alpaca Data API.

    Credentials are passed in per tenant; this object never reads the environment,
    so a data provider built for one client can never authenticate as another.
    """

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        *,
        base_url: str = "https://data.alpaca.markets",
        feed: str = "iex",
        timeout: float = 10.0,
    ) -> None:
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        }
        self._base_url = base_url.rstrip("/")
        self._feed = feed
        self._timeout = timeout

    def _get(self, path: str, params: Mapping[str, object]) -> dict:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("httpx is required for live market data") from exc
        response = httpx.get(
            f"{self._base_url}{path}",
            headers=self._headers,
            params=dict(params),
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    def history(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        payload = self._get(
            f"/v2/stocks/{symbol.upper()}/bars",
            {"timeframe": timeframe, "limit": limit, "feed": self._feed},
        )
        return [_parse_bar(item) for item in payload.get("bars") or []]

    def last_price(self, symbol: str) -> float:
        payload = self._get(
            f"/v2/stocks/{symbol.upper()}/trades/latest", {"feed": self._feed}
        )
        return float(payload["trade"]["p"])


def _parse_bar(item: Mapping[str, object]) -> Bar:
    """Validate one bar from the vendor payload at the system boundary."""
    return Bar(
        ts=datetime.fromisoformat(str(item["t"]).replace("Z", "+00:00")),
        open=float(item["o"]),
        high=float(item["h"]),
        low=float(item["l"]),
        close=float(item["c"]),
        volume=float(item["v"]),
    )


def latest_prices(
    provider: MarketDataProvider, symbols: Sequence[str]
) -> dict[str, float]:
    return {symbol.upper(): provider.last_price(symbol) for symbol in symbols}

"""Market regime — is the broad market risk-on or risk-off right now.

The single most durable filter in trend/breakout trading is not a better entry
signal, it is refusing to take certain setups while the index is below its long
moving average. Most failed breakouts happen in a falling market; skipping them
removes a large share of the losses without needing to predict anything.

Deliberately computed *outside* the Risk Manager and passed in as a fact, the same
way the account snapshot is. The Risk Manager stays free of network calls and
non-determinism, which is what makes it auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tradesys.data import indicators
from tradesys.data.market_data import MarketDataProvider


@dataclass(frozen=True, slots=True)
class MarketRegime:
    """A read of the broad market at one moment."""

    symbol: str
    price: float
    average: float
    period: int
    as_of: datetime

    @property
    def risk_on(self) -> bool:
        return self.price >= self.average

    @property
    def distance_pct(self) -> float:
        if self.average <= 0:
            return 0.0
        return (self.price / self.average - 1) * 100

    def describe(self) -> str:
        state = "risk-on" if self.risk_on else "risk-off"
        return (
            f"{self.symbol} {self.price:.2f} vs {self.period}-bar average "
            f"{self.average:.2f} ({self.distance_pct:+.2f}%) — {state}"
        )


def assess_regime(
    market_data: MarketDataProvider,
    symbol: str,
    *,
    period: int,
    timeframe: str,
    now: datetime,
) -> MarketRegime | None:
    """Read the regime, or None when there is not enough history to judge.

    None means "unknown", and the Risk Manager treats unknown as blocking for the
    setups gated on regime. A filter that quietly passes when its data is missing
    is not a filter.
    """
    if period < 2:
        raise ValueError("regime period must be at least 2 bars")

    try:
        bars = market_data.history(symbol, timeframe=timeframe, limit=period + 10)
    except Exception:
        return None

    if len(bars) < period:
        return None

    closes = indicators.closes(bars)
    average = indicators.sma(closes, period)
    if average is None:
        return None

    return MarketRegime(
        symbol=symbol.upper(),
        price=closes[-1],
        average=average,
        period=period,
        as_of=now,
    )

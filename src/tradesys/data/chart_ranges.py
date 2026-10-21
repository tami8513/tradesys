"""Time ranges for the client's chart, and the long-run history behind a symbol.

One ten-year fetch of daily bars serves every range from a month up: the month,
half-year and year ranges slice it, and the five- and ten-year ranges fold it into
weekly bars. Only the day and week ranges need intraday bars of their own.
Alpaca's consolidated history starts in 2016, so ten years is the deepest view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Sequence
from zoneinfo import ZoneInfo

from tradesys.core.models import Bar

NEW_YORK = ZoneInfo("America/New_York")
TEN_YEARS_OF_DAYS = 2600

RANGES = ("יום", "שבוע", "חודש", "חצי שנה", "שנה", "5 שנים", "10 שנים")
INTRADAY = {"יום": ("5Min", 1, 400), "שבוע": ("30Min", 5, 200)}   # timeframe, sessions, bars
DAILY = {"חודש": 22, "חצי שנה": 126, "שנה": 252}
WEEKLY = {"5 שנים": 261, "10 שנים": 522}


def regular_session(bars: Sequence[Bar], sessions: int) -> list[Bar]:
    """Bars inside 9:30-16:00 New York time, for the last `sessions` trading days."""
    inside = [
        b for b in bars
        if time(9, 30) <= b.ts.astimezone(NEW_YORK).time() < time(16, 0)
    ]
    days = sorted({b.ts.astimezone(NEW_YORK).date() for b in inside})[-sessions:]
    return [b for b in inside if b.ts.astimezone(NEW_YORK).date() in days]


def weekly(bars: Sequence[Bar]) -> list[Bar]:
    """Fold daily bars into weekly ones, each stamped with its first day."""
    weeks: list[Bar] = []
    current: tuple[int, int] | None = None
    for bar in bars:
        week = bar.ts.isocalendar()[:2]
        if week != current:
            weeks.append(bar)
            current = week
            continue
        first = weeks[-1]
        weeks[-1] = Bar(
            ts=first.ts,
            open=first.open,
            high=max(first.high, bar.high),
            low=min(first.low, bar.low),
            close=bar.close,
            volume=first.volume + bar.volume,
        )
    return weeks


@dataclass(frozen=True, slots=True)
class History:
    years: list[tuple[int, float]]   # calendar year -> price return %, the last one so far
    total: float                     # % over the whole history
    per_year: float                  # compound yearly %
    max_drawdown: float              # deepest fall from a peak, % (negative)
    up_years: int                    # among full calendar years
    full_years: int
    first_day: str


def history(bars: Sequence[Bar]) -> History | None:
    """Year-by-year research on a daily series (price only, without dividends)."""
    if len(bars) < 2:
        return None
    years: list[tuple[int, float]] = []
    base = bars[0].close
    for i, bar in enumerate(bars):
        last_of_year = i + 1 == len(bars) or bars[i + 1].ts.year != bar.ts.year
        if last_of_year:
            years.append((bar.ts.year, (bar.close / base - 1) * 100))
            base = bar.close

    peak, deepest = bars[0].close, 0.0
    for bar in bars:
        peak = max(peak, bar.close)
        deepest = min(deepest, (bar.close / peak - 1) * 100)

    span_years = (bars[-1].ts - bars[0].ts).days / 365.25
    growth = bars[-1].close / bars[0].close
    complete = years[1:-1] if len(years) > 2 else years
    return History(
        years=years,
        total=(growth - 1) * 100,
        per_year=(growth ** (1 / span_years) - 1) * 100 if span_years >= 1 else (growth - 1) * 100,
        max_drawdown=deepest,
        up_years=sum(r > 0 for _, r in complete),
        full_years=len(complete),
        first_day=bars[0].ts.date().isoformat(),
    )

"""The money-flow agent: is money pouring into a stock, with little selling against it?

From daily bars it reads how heavily a stock traded against its usual volume,
whether it closed near the day's high (buyers won the day) or near the low,
the 20-day money flow (Chaikin), and how many heavy-volume up days
(accumulation) and down days (distribution) the last 25 sessions held.

Ten years of the 101 approved leaders (2016-2026) found that unusual buying
alone did not lead to better returns: a month later the price was up 55.9%
of the time against 57.8% on an ordinary day, and sustained accumulation did
no better. Heavy selling was followed by more gains (a rebound). So this agent
does not treat buying as good news by itself: it checks what unusual buying
did on this particular stock before, and only that evidence moves a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from tradesys.agents.chart_analyst import Evidence
from tradesys.core.models import Bar

UNUSUAL_VOLUME = 2.0      # times the 20-day average
CLOSE_NEAR_EDGE = 0.5     # close location, -1 at the low to +1 at the high
FORWARD_DAYS = 21
SPACING_DAYS = 10
FLOW_DAYS = 20
COUNT_DAYS = 25
MIN_BARS = 60


def _location(bar: Bar) -> float:
    """Where the close sits in the day's range: -1 at the low, +1 at the high."""
    span = bar.high - bar.low
    return ((bar.close - bar.low) - (bar.high - bar.close)) / span if span > 0 else 0.0


def _relative_volume(bars: Sequence[Bar], i: int) -> float:
    window = bars[max(0, i - FLOW_DAYS):i]
    average = sum(b.volume for b in window) / len(window) if window else 0.0
    return bars[i].volume / average if average else 0.0


def _buying(bars: Sequence[Bar], i: int) -> bool:
    return (_relative_volume(bars, i) >= UNUSUAL_VOLUME
            and bars[i].close > bars[i - 1].close
            and _location(bars[i]) >= CLOSE_NEAR_EDGE)


def _selling(bars: Sequence[Bar], i: int) -> bool:
    return (_relative_volume(bars, i) >= UNUSUAL_VOLUME
            and bars[i].close < bars[i - 1].close
            and _location(bars[i]) <= -CLOSE_NEAR_EDGE)


@dataclass(frozen=True, slots=True)
class FlowReading:
    relative_volume: float
    close_location: float
    money_flow: float          # 20-day Chaikin money flow, -1 to +1
    accumulation_days: int     # heavy-volume up days in the last 25 sessions
    distribution_days: int     # heavy-volume down days in the last 25 sessions
    unusual_buying: bool
    unusual_selling: bool
    evidence: Evidence | None  # what unusual buying did on this stock before


def _evidence(bars: Sequence[Bar]) -> Evidence:
    every, after_buying = [], []
    last = -SPACING_DAYS
    for i in range(FLOW_DAYS + 1, len(bars) - FORWARD_DAYS):
        forward = (bars[i + FORWARD_DAYS].close / bars[i].close - 1) * 100
        every.append(forward)
        if i - last >= SPACING_DAYS and _buying(bars, i):
            after_buying.append(forward)
            last = i

    def rate(xs: list[float]) -> float:
        return sum(x > 0 for x in xs) / len(xs) * 100 if xs else 0.0

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return Evidence(len(after_buying), rate(after_buying), mean(after_buying), rate(every), mean(every))


def read_flow(bars: Sequence[Bar], *, with_evidence: bool = True) -> FlowReading | None:
    if len(bars) < MIN_BARS:
        return None
    i = len(bars) - 1
    window = bars[-FLOW_DAYS:]
    volume = sum(b.volume for b in window)
    money_flow = sum(_location(b) * b.volume for b in window) / volume if volume else 0.0
    accumulation = distribution = 0
    for j in range(len(bars) - COUNT_DAYS, len(bars)):
        heavy = bars[j].volume > sum(b.volume for b in bars[j - 50:j]) / 50 if j >= 50 else False
        accumulation += heavy and bars[j].close > bars[j - 1].close * 1.002
        distribution += heavy and bars[j].close < bars[j - 1].close * 0.998
    return FlowReading(
        relative_volume=_relative_volume(bars, i),
        close_location=_location(bars[i]),
        money_flow=money_flow,
        accumulation_days=accumulation,
        distribution_days=distribution,
        unusual_buying=_buying(bars, i),
        unusual_selling=_selling(bars, i),
        evidence=_evidence(bars) if with_evidence else None,
    )


def describe(flow: FlowReading) -> list[str]:
    """Plain Hebrew lines about the money flow."""
    lines = []
    if flow.unusual_buying:
        lines.append(f"קנייה חריגה היום: נסחרה פי {flow.relative_volume:.1f} מהרגיל ונסגרה קרוב לשיא "
                     "היום, כלומר הרבה קונים ומעט מוכרים")
    elif flow.unusual_selling:
        lines.append(f"מכירה חריגה היום: נסחרה פי {flow.relative_volume:.1f} מהרגיל ונסגרה קרוב לשפל היום")
    direction = "נכנס" if flow.money_flow > 0.05 else "יוצא" if flow.money_flow < -0.05 else "מאוזן"
    lines.append(f"זרימת הכסף ב-20 הימים האחרונים: {direction} ({flow.money_flow:+.2f}) · "
                 f"{flow.accumulation_days} ימי קנייה כבדה מול {flow.distribution_days} ימי מכירה כבדה בחודש")
    return lines


def flow_points(flow: FlowReading) -> tuple[int, str] | None:
    """Points for a verdict, only when this stock's own history backs the reading."""
    if not flow.unusual_buying:
        return None
    evidence = flow.evidence
    if evidence is None or evidence.cases < 8:
        return 0, "קנייה חריגה היום, אבל אין מספיק מקרים דומים במניה הזו כדי לדעת אם זה סימן טוב"
    edge = evidence.win_rate - evidence.base_win_rate
    summary = (f"אחרי קנייה חריגה בעבר ({evidence.cases} פעמים) המחיר עלה תוך חודש ב-"
               f"{evidence.win_rate:.0f}% מהמקרים, מול {evidence.base_win_rate:.0f}% ביום רגיל")
    if edge >= 5:
        return 1, f"קנייה חריגה היום, ובמניה הזו זה עבד: {summary}"
    if edge <= -5:
        return -1, f"קנייה חריגה היום, אבל במניה הזו זה דווקא עבד פחות טוב: {summary}"
    return 0, f"קנייה חריגה היום, אבל במניה הזו זה לא שינה הרבה: {summary}"

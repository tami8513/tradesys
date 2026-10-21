"""The chart analyst: reads a daily chart and says whether now is a time to enter.

It classifies today's chart into one of six readings (pullback in an uptrend,
breakout, healthy trend, stretched, no trend, downtrend), proposes an entry zone
and a stop, and then checks its own claim against the symbol's history: every
earlier day with the same reading, and what the price did over the next month.
That track record is the point. A reading that has not worked on this symbol
says so, instead of sounding as confident as one that has.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from tradesys.core.models import Bar

FORWARD_DAYS = 21           # one trading month
SPACING_DAYS = 10           # one episode is counted once, not on each of its days
MIN_HISTORY = 220
MIN_CASES = 5

LEADING_INDICES: tuple[tuple[str, str], ...] = (
    ("QQQ", "נאסד\"ק 100"),
    ("SPY", "S&P 500"),
    ("DIA", "דאו ג'ונס"),
    ("IWM", "ראסל 2000"),
    ("SMH", "שבבים"),
)

VERDICTS = {
    "pullback": ("enter", "נקודת כניסה טובה: תיקון בתוך מגמת עלייה"),
    "breakout": ("enter", "פריצה: שיא של 3 חודשים עם מחזור גבוה"),
    "trend": ("maybe", "מגמת עלייה בריאה, אבל זו לא נקודה מיוחדת"),
    "stretched": ("wait", "לחכות: עלתה מהר מדי, עדיף לחכות לתיקון"),
    "sideways": ("wait", "אין מגמה ברורה, לחכות לכיוון"),
    "downtrend": ("avoid", "לא להיכנס: מגמת ירידה"),
}


@dataclass(slots=True)
class Evidence:
    cases: int
    win_rate: float          # % of cases higher a month later
    average: float           # average return a month later, %
    base_win_rate: float     # the same over every day, for comparison
    base_average: float


@dataclass(slots=True)
class ChartReading:
    symbol: str
    signal: str
    action: str              # enter / maybe / wait / avoid
    headline: str
    price: float
    entry_zone: tuple[float, float]
    stop: float
    target: float | None
    plan: str = ""
    breakout: float = 0.0    # the 3-month high: a close above it on heavy volume is a breakout
    facts: list[str] = field(default_factory=list)
    evidence: Evidence | None = None


# ------------------------------------------------------------- series math

def _sma_series(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def _rsi_series(values: Sequence[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    gain, loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    for i in range(period, len(values)):
        if i > period:
            gain = (gain * (period - 1) + gains[i - 1]) / period
            loss = (loss * (period - 1) + losses[i - 1]) / period
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def _atr_series(bars: Sequence[Bar], period: int = 14) -> list[float | None]:
    ranges = [bars[0].high - bars[0].low] + [
        max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        for p, b in zip(bars, bars[1:])
    ]
    return _sma_series(ranges, period)


class _Series:
    def __init__(self, bars: Sequence[Bar]) -> None:
        self.close = [b.close for b in bars]
        self.volume = [b.volume for b in bars]
        self.sma20 = _sma_series(self.close, 20)
        self.sma50 = _sma_series(self.close, 50)
        self.sma200 = _sma_series(self.close, 200)
        self.volume50 = _sma_series(self.volume, 50)
        self.rsi = _rsi_series(self.close)
        self.atr = _atr_series(bars)

    def signal(self, i: int) -> str:
        c, s50, s200, rsi = self.close[i], self.sma50[i], self.sma200[i], self.rsi[i]
        if c < s200 and s50 < s200:
            return "downtrend"
        if not (c > s200 and s50 > s200):
            return "sideways"
        high_3m = max(self.close[i - 63:i])
        if c > high_3m and self.volume[i] >= 1.5 * (self.volume50[i] or 0.0):
            return "breakout"
        if c >= s50 * 1.08 or rsi >= 70:
            return "stretched"
        if c <= s50 * 1.03 or rsi <= 45:
            return "pullback"
        return "trend"


# ----------------------------------------------------------------- reading

def trend_view(bars: Sequence[Bar]) -> tuple[str, bool] | None:
    """Today's reading and whether a death cross is on (50-day under 200-day, price under both)."""
    if len(bars) < MIN_HISTORY:
        return None
    series = _Series(bars)
    i = len(bars) - 1
    death_cross = series.sma50[i] < series.sma200[i] and series.close[i] < series.sma200[i]
    return series.signal(i), death_cross


def _evidence(series: _Series, signal: str) -> Evidence:
    every, same = [], []
    last_counted = -SPACING_DAYS
    for i in range(200, len(series.close) - FORWARD_DAYS):
        forward = (series.close[i + FORWARD_DAYS] / series.close[i] - 1) * 100
        every.append(forward)
        if i - last_counted >= SPACING_DAYS and series.signal(i) == signal:
            same.append(forward)
            last_counted = i

    def rate(xs: list[float]) -> float:
        return sum(x > 0 for x in xs) / len(xs) * 100 if xs else 0.0

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return Evidence(len(same), rate(same), mean(same), rate(every), mean(every))


def analyze(symbol: str, bars: Sequence[Bar]) -> ChartReading | None:
    """A reading of the latest daily bar, or None when the history is too short."""
    if len(bars) < MIN_HISTORY:
        return None
    s = _Series(bars)
    i = len(bars) - 1
    signal = s.signal(i)
    action, headline = VERDICTS[signal]
    price, atr = s.close[i], s.atr[i] or 0.0
    s20, s50, s200 = s.sma20[i], s.sma50[i], s.sma200[i]

    if action == "enter":
        zone = (price - 0.5 * atr, price)
    elif signal in ("trend", "stretched"):
        support = s20 if signal == "trend" else s50
        zone = (support - 0.5 * atr, support + 0.5 * atr)
    elif signal == "downtrend":
        zone = (s200, s200 * 1.02)
    else:
        zone = (s50, s50 + atr)
    entry = zone[1]
    stop = entry - 2.5 * atr
    target = entry + 2 * (entry - stop) if action in ("enter", "maybe") else None
    low, high = f"{zone[0]:,.2f}", f"{zone[1]:,.2f}"
    plan = {
        "enter": f"אזור כניסה: {low}-{high} · סטופ {stop:,.2f}"
                 + (f" · יעד ראשון {target:,.2f}" if target else ""),
        "maybe": f"עדיף לחכות לירידה לאזור {low}-{high} (ממוצע 20 יום) · סטופ {stop:,.2f}",
        "wait": (f"לחכות לירידה לאזור {low}-{high} (ממוצע 50 יום)" if signal == "stretched"
                 else f"להיכנס רק אחרי חזרה מעל {low} (ממוצע 50 יום)"),
        "avoid": f"להיכנס רק אחרי חזרה מעל {low} (ממוצע 200 יום)",
    }[action]

    year = s.close[-252:]
    off_high = (price / max(year) - 1) * 100
    trend = (
        "מעל הממוצע של 200 יום, והממוצע של 50 מעליו: מגמת עלייה"
        if s50 > s200 and price > s200
        else "מתחת לממוצע של 200 יום: מגמת ירידה"
        if price < s200 and s50 < s200
        else "סביב הממוצע של 200 יום: אין מגמה ברורה"
    )
    rsi = s.rsi[i] or 50.0
    mood = "קנו אותה הרבה לאחרונה" if rsi >= 70 else "מכרו אותה הרבה לאחרונה" if rsi <= 30 else "מאוזן"
    facts = [
        f"מחיר {price:,.2f} · {trend}",
        f"{off_high:+.1f}% מהשיא של השנה האחרונה ({max(year):,.2f})",
        f"מדד עוצמה (RSI) {rsi:.0f}: {mood}",
        _levels(price, {"ממוצע 50": s50, "ממוצע 200": s200, "שיא 3 חודשים": max(s.close[i - 63:i])}),
        f"תנועה יומית ממוצעת {atr / price * 100:.1f}%",
    ]
    return ChartReading(
        symbol=symbol,
        signal=signal,
        action=action,
        headline=headline,
        price=price,
        entry_zone=zone,
        stop=stop,
        target=target,
        plan=plan,
        breakout=max(s.close[i - 63:i]),
        facts=facts,
        evidence=_evidence(s, signal),
    )


def _levels(price: float, levels: dict[str, float]) -> str:
    """Levels below the price are support, levels above it are resistance."""
    below = [f"{name} ב-{value:,.2f}" for name, value in levels.items() if value <= price]
    above = [f"{name} ב-{value:,.2f}" for name, value in levels.items() if value > price]
    parts = []
    if below:
        parts.append("תמיכה: " + ", ".join(below))
    if above:
        parts.append("התנגדות: " + ", ".join(above))
    return " · ".join(parts)


def evidence_text(evidence: Evidence | None) -> str:
    if evidence is None or evidence.cases < MIN_CASES:
        return "אין מספיק מקרים דומים בהיסטוריה של המניה כדי לבדוק את הקריאה הזו."
    edge = evidence.win_rate - evidence.base_win_rate
    verdict = (
        "הקריאה הזו עבדה על המניה הזו יותר מהרגיל"
        if edge >= 5
        else "הקריאה הזו לא עבדה על המניה הזו טוב יותר מסתם יום רגיל"
        if edge > -5
        else "הקריאה הזו עבדה על המניה הזו פחות טוב מהרגיל"
    )
    return (
        f"בדקתי את ההיסטוריה: {evidence.cases} פעמים היה מצב דומה. אחרי חודש המחיר היה "
        f"גבוה יותר ב-{evidence.win_rate:.0f}% מהמקרים, בממוצע {evidence.average:+.1f}%. "
        f"ביום רגיל: {evidence.base_win_rate:.0f}% מהמקרים, בממוצע {evidence.base_average:+.1f}%. "
        f"{verdict}."
    )

"""The valuation agent: what investors pay for a dollar of profit, and is that cheap or dear.

A P/E alone says little: 40 is cheap for a company doubling its earnings and
dear for one that stands still. So the agent reads it four ways: today's P/E
(trailing twelve months), the forward P/E on next year's expected profit, the
P/E per point of growth (PEG), and today's P/E against the stock's own
five-year median. Only the extremes move a verdict; valuation has not been
validated as an edge in this system, so the middle is information only.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

# Vendors spell these differently across versions; the first present wins.
TRAILING_KEYS = ("peTTM", "peExclExtraTTM", "peBasicExclExtraTTM", "peInclExtraTTM")
FORWARD_KEYS = ("forwardPE", "forwardPe", "peForward")
GROWTH_KEYS = ("epsGrowth5Y", "epsGrowthTTMYoy")
CHEAP_PEG, DEAR_PEG = 1.0, 2.5
CHEAP_VS_HISTORY, DEAR_VS_HISTORY = 0.8, 1.3
MIN_HISTORY = 8               # quarters of P/E history before comparing with it


@dataclass(frozen=True, slots=True)
class Valuation:
    pe: float | None
    forward_pe: float | None
    peg: float | None
    pe_median: float | None       # the stock's own median P/E over the history given
    losing_money: bool

    @property
    def implied_growth(self) -> float | None:
        """Profit growth, in %, the market prices in going from today's P/E to next year's."""
        if self.pe and self.forward_pe and self.pe > 0 and self.forward_pe > 0:
            return (self.pe / self.forward_pe - 1) * 100
        return None

    def summary(self) -> str:
        parts = []
        if self.losing_money:
            parts.append("מפסידה כרגע, אין מכפיל רווח")
        elif self.pe:
            parts.append(f"מכפיל רווח {self.pe:.1f}")
        if self.forward_pe and self.forward_pe > 0:
            parts.append(f"מכפיל עתידי {self.forward_pe:.1f}")
        if self.peg:
            parts.append(f"PEG {self.peg:.1f}")
        if self.pe_median:
            parts.append(f"ממוצע 5 שנים {self.pe_median:.1f}")
        return " · ".join(parts)


def _first(metrics: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = metrics.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def read_valuation(metrics: dict | None, pe_history: list[float] | None = None) -> Valuation | None:
    """From the vendor's metrics (and quarterly P/E history), or None with no P/E at all."""
    if not metrics:
        return None
    pe = _first(metrics, TRAILING_KEYS)
    forward = _first(metrics, FORWARD_KEYS)
    if pe is None and forward is None:
        return None
    losing = pe is not None and pe <= 0
    growth = _first(metrics, GROWTH_KEYS)
    peg = pe / growth if pe and pe > 0 and growth and growth > 0 else None
    history = [v for v in (pe_history or []) if v > 0]
    return Valuation(
        pe=None if losing else pe,
        forward_pe=forward,
        peg=peg,
        pe_median=median(history) if len(history) >= MIN_HISTORY else None,
        losing_money=losing,
    )


def judge(v: Valuation) -> tuple[list[str], list[str], int]:
    """(strengths, weaknesses, points): points only at the extremes."""
    strengths: list[str] = []
    weaknesses: list[str] = []
    points = 0
    if v.losing_money:
        weaknesses.append("החברה מפסידה כרגע, ולכן אין לה מכפיל רווח: המחיר נשען על ציפיות בלבד")
    growth = v.implied_growth
    if growth is not None and growth >= 5:
        strengths.append(f"השוק מצפה שהרווח יגדל בכ-{growth:.0f}% בשנה הקרובה "
                         f"(מכפיל עתידי {v.forward_pe:.1f} מול {v.pe:.1f} היום)")
    elif growth is not None and growth <= -5:
        weaknesses.append(f"השוק מצפה לירידה של כ-{-growth:.0f}% ברווח בשנה הקרובה "
                          f"(מכפיל עתידי {v.forward_pe:.1f} מול {v.pe:.1f} היום)")
    if v.peg is not None and v.peg < CHEAP_PEG:
        strengths.append(f"זולה ביחס לצמיחה שלה (PEG {v.peg:.1f}, מתחת ל-1)")
    elif v.peg is not None and v.peg > DEAR_PEG:
        weaknesses.append(f"יקרה ביחס לצמיחה שלה (PEG {v.peg:.1f})")
    ratio = v.pe / v.pe_median if v.pe and v.pe_median else None
    if ratio is not None and ratio < CHEAP_VS_HISTORY:
        strengths.append(f"זולה ביחס לעבר שלה: מכפיל {v.pe:.1f} מול {v.pe_median:.1f} בממוצע ב-5 השנים")
    elif ratio is not None and ratio > DEAR_VS_HISTORY:
        weaknesses.append(f"יקרה ביחס לעבר שלה: מכפיל {v.pe:.1f} מול {v.pe_median:.1f} בממוצע ב-5 השנים")
    cheap = (v.peg is not None and v.peg < CHEAP_PEG) and (ratio is not None and ratio < CHEAP_VS_HISTORY)
    dear = (v.peg is not None and v.peg > DEAR_PEG) and (ratio is not None and ratio > DEAR_VS_HISTORY)
    if cheap:
        points = 1
    elif dear or v.losing_money:
        points = -1
    return strengths, weaknesses, points

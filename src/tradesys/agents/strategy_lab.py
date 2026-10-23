"""The strategy lab: which way of trading one stock would have worked, and does it still.

It replays the robot's own rules on the stock's last five years, day by day:
entries from the Scout's `detect_setups` (the same function the robot trades
with), exits as the PositionMonitor runs them (ATR stop, a stop that trails
from +1R, and holding past the target while the trend is up). It tries three
styles of entry and three stop distances, picks the best on the first four
years, and then scores that choice on the fifth year, which played no part in
choosing it. A plan that only looks good on the years it was picked on is
reported as such.

Each trade puts the whole test amount in; the robot's real sizing is smaller,
so these numbers describe the method, not the size of a real position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from tradesys.agents.monitor import TREND_SLOPE_BARS, TREND_SMA_PERIOD
from tradesys.agents.scout import TARGET_R_MULTIPLE, detect_setups
from tradesys.core.enums import SetupType
from tradesys.core.models import Bar
from tradesys.data.indicators import sma

LOOKBACK = 180               # bars the Scout reads, as in the profiles
YEARS = 5
TEST_DAYS = 252
COST = 0.0005                # per side: spread and slippage
TRAIL_ACTIVATION_R = 1.0
TRAIL_DISTANCE_R = 1.0
STOPS = (2.0, 3.0, 4.0)
STYLES: dict[str, tuple[str, frozenset[SetupType]]] = {
    "dips": ("מחכה לירידות", frozenset({SetupType.PULLBACK, SetupType.OVERSOLD_REVERSAL})),
    "trend": ("רוכב על מגמות", frozenset({SetupType.TREND_CONTINUATION, SetupType.BREAKOUT})),
    "both": ("משלב ירידות ומגמות", frozenset(SetupType)),
}


@dataclass(frozen=True, slots=True)
class Result:
    total: float             # % over the period
    max_drawdown: float      # % (negative)
    trades: int
    win_rate: float          # % of trades that made money
    exposure: float          # % of days holding the stock


@dataclass(frozen=True, slots=True)
class Plan:
    style: str
    stop: float
    train: Result
    test: Result

    @property
    def label(self) -> str:
        return f"{STYLES[self.style][0]}, סטופ {self.stop:g} תנודות יומיות"

    @property
    def setups(self) -> frozenset[SetupType]:
        return STYLES[self.style][1]


@dataclass(frozen=True, slots=True)
class LabReport:
    best: Plan
    plans: list[Plan]
    hold_train: Result
    hold_test: Result
    first_day: str
    split_day: str

    @property
    def holds_up(self) -> bool:
        """The chosen plan made money on the year it was not picked on."""
        return self.best.test.total > 0

    @property
    def beats_holding(self) -> bool:
        return self.best.train.total > self.hold_train.total and self.best.test.total > self.hold_test.total


def _rising_trend(closes: Sequence[float]) -> bool:
    current = sma(closes, TREND_SMA_PERIOD)
    earlier = sma(closes[:-TREND_SLOPE_BARS], TREND_SMA_PERIOD)
    return current is not None and earlier is not None and closes[-1] > current > earlier


def _signals(bars: Sequence[Bar], start: int) -> dict[int, tuple[set[SetupType], float]]:
    """Setups and ATR on each day from `start`, read once and shared by every variant."""
    found = {}
    for i in range(start, len(bars)):
        detected = detect_setups(bars[max(0, i + 1 - LOOKBACK): i + 1])
        if detected and detected[0]:
            found[i] = ({s.setup for s in detected[0]}, detected[1])
    return found


def simulate(
    bars: Sequence[Bar],
    signals: dict[int, tuple[set[SetupType], float]],
    setups: frozenset[SetupType],
    stop_multiple: float,
    start: int,
    end: int,
) -> Result:
    """One position at a time, all in, from day `start` to `end` (exclusive)."""
    equity, peak_equity, deepest = 1.0, 1.0, 0.0
    shares = entry = stop = target = peak = risk = 0.0
    in_trade, held_days, wins, trades = False, 0, 0, 0

    def exit_at(price: float) -> None:
        nonlocal equity, in_trade, wins, trades
        equity = shares * price * (1 - COST)
        trades += 1
        wins += price > entry
        in_trade = False

    for i in range(start, end):
        bar = bars[i]
        if in_trade:
            held_days += 1
            if bar.open <= stop:
                exit_at(bar.open)
            elif bar.low <= stop:
                exit_at(stop)
            else:
                peak = max(peak, bar.high)
                if peak >= entry + TRAIL_ACTIVATION_R * risk:
                    stop = max(stop, entry, peak - TRAIL_DISTANCE_R * risk)
                if target and bar.high >= target:
                    window = bars[max(0, i + 1 - TREND_SMA_PERIOD - TREND_SLOPE_BARS): i + 1]
                    if _rising_trend([b.close for b in window]):
                        stop = max(stop, entry + 0.5 * (target - entry))
                        target = 0.0
                    else:
                        exit_at(target)
        if not in_trade and i in signals and signals[i][0] & setups and i + 1 < end:
            entry = bar.close
            risk = stop_multiple * signals[i][1]
            stop = entry - risk
            target = entry + TARGET_R_MULTIPLE * (entry - stop)
            peak = entry
            shares = equity * (1 - COST) / entry
            in_trade = True

        value = shares * bar.close if in_trade else equity
        peak_equity = max(peak_equity, value)
        deepest = min(deepest, value / peak_equity - 1)
    if in_trade:
        exit_at(bars[end - 1].close)
    days = max(1, end - start)
    return Result((equity - 1) * 100, deepest * 100, trades,
                  wins / trades * 100 if trades else 0.0, held_days / days * 100)


def _hold(bars: Sequence[Bar], start: int, end: int) -> Result:
    first, top, deepest = bars[start].close, bars[start].close, 0.0
    for bar in bars[start:end]:
        top = max(top, bar.close)
        deepest = min(deepest, bar.close / top - 1)
    return Result((bars[end - 1].close / first - 1) * 100, deepest * 100, 1, 0.0, 100.0)


def score(result: Result) -> float:
    """Return per unit of pain: a plan that doubles with a 60% fall is not the best."""
    return result.total / max(10.0, -result.max_drawdown)


def run_lab(bars: Sequence[Bar]) -> LabReport | None:
    """Needs five years of daily bars plus the Scout's look-back."""
    if len(bars) < LOOKBACK + YEARS * 252 // 2:
        return None
    start = max(LOOKBACK, len(bars) - YEARS * 252)
    split = len(bars) - TEST_DAYS
    signals = _signals(bars, start)
    plans = [
        Plan(
            style, stop,
            simulate(bars, signals, STYLES[style][1], stop, start, split),
            simulate(bars, signals, STYLES[style][1], stop, split, len(bars)),
        )
        for style in STYLES for stop in STOPS
    ]
    best = max(plans, key=lambda p: score(p.train))
    return LabReport(
        best=best,
        plans=plans,
        hold_train=_hold(bars, start, split),
        hold_test=_hold(bars, split, len(bars)),
        first_day=bars[start].ts.date().isoformat(),
        split_day=bars[split].ts.date().isoformat(),
    )

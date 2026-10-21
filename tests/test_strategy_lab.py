from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from pytest import approx

from tradesys.agents.strategy_lab import COST, STOPS, STYLES, run_lab, simulate
from tradesys.core.enums import SetupType
from tradesys.core.models import Bar

START = datetime(2021, 1, 4, tzinfo=timezone.utc)
PULL = {SetupType.PULLBACK}
DIPS = STYLES["dips"][1]


def bar(i: int, close: float, low: float | None = None, high: float | None = None,
        open_: float | None = None) -> Bar:
    return Bar(START + timedelta(days=i), open_ or close, high or close, low or close, close, 1e6)


def test_a_gap_below_the_stop_exits_at_the_open_not_the_stop():
    bars = [bar(0, 100), bar(1, 100), bar(2, 80, low=78, open_=85)]
    result = simulate(bars, {0: (PULL, 5.0)}, DIPS, 2.0, 0, 3)     # stop at 90

    assert result.trades == 1
    assert result.total == approx(((1 - COST) * 0.85 * (1 - COST) - 1) * 100)


def test_the_stop_trails_from_one_r_and_locks_the_gain():
    # Entry 100, ATR 5, stop 2 ATR = 90, so 1R = 10 and the target is 125.
    # A run to 124 trails the stop to 114; the fall to 110 sells there.
    bars = [bar(0, 100), bar(1, 112, high=112), bar(2, 120, high=124), bar(3, 110, low=110, open_=116)]
    result = simulate(bars, {0: (PULL, 5.0)}, DIPS, 2.0, 0, 4)

    assert result.trades == 1 and result.win_rate == 100
    assert result.total == approx(((1 - COST) * 1.14 * (1 - COST) - 1) * 100)


def test_only_the_chosen_entry_style_trades():
    bars = [bar(i, 100 + i) for i in range(5)]
    signals = {0: ({SetupType.TREND_CONTINUATION}, 1.0)}

    assert simulate(bars, signals, DIPS, 2.0, 0, 5).trades == 0
    assert simulate(bars, signals, STYLES["trend"][1], 2.0, 0, 5).trades == 1


def wavy(n: int) -> list[Bar]:
    closes = [100 * 1.0007**i * (1 + 0.08 * math.sin(i / 11)) for i in range(n)]
    return [bar(i, c, low=c * 0.99, high=c * 1.01) for i, c in enumerate(closes)]


def test_the_lab_picks_on_four_years_and_scores_on_the_fifth():
    report = run_lab(wavy(1500))

    assert len(report.plans) == len(STYLES) * len(STOPS)
    assert report.split_day > report.first_day
    best_train = max(p.train.total / max(10.0, -p.train.max_drawdown) for p in report.plans)
    assert report.best.train.total / max(10.0, -report.best.train.max_drawdown) == best_train


def test_too_little_history_gives_no_lab_report():
    assert run_lab(wavy(400)) is None


def test_at_the_target_it_sells_unless_the_trend_is_still_rising():
    falling = [bar(i, 130 - i) for i in range(20)]
    run = falling + [bar(20, 100), bar(21, 126, high=126, open_=110), bar(22, 126)]
    result = simulate(run, {20: (PULL, 5.0)}, DIPS, 2.0, 20, 23)
    assert result.total == approx(((1 - COST) * 1.25 * (1 - COST) - 1) * 100)

    rising = [bar(i, 80 + i) for i in range(20)]
    run = rising + [bar(20, 100), bar(21, 126, high=126, open_=110), bar(22, 130)]
    held = simulate(run, {20: (PULL, 5.0)}, DIPS, 2.0, 20, 23)
    assert held.total == approx(((1 - COST) * 1.30 * (1 - COST) - 1) * 100)

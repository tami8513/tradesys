from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import make_profile

from tradesys.backtest import BENCHMARK, ReplayMarketData, _high_pass, _stop_pass, run_backtest
from tradesys.core.enums import SetupType, Side
from tradesys.core.models import Bar, TradeRecord

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def series(closes: list[float]) -> list[Bar]:
    return [
        Bar(START + timedelta(days=i), c, c * 1.01, c * 0.99, c, 2_000_000)
        for i, c in enumerate(closes)
    ]


def test_replay_never_reveals_a_future_bar():
    market = ReplayMarketData({"RIVN": series([10, 11, 12, 13])})
    market.set_day(date(2024, 1, 2))
    seen = market.history("RIVN", timeframe="1Day", limit=10)
    assert [b.close for b in seen] == [10, 11]
    assert market.last_price("RIVN") == 11


def trade(stop=18.0, target=26.0) -> TradeRecord:
    return TradeRecord(
        trade_id=1, client_id="tal_dahan", symbol="RIVN", setup=SetupType.BREAKOUT,
        side=Side.BUY, qty=10, entry_price=20.0, stop_price=stop, opened_at=START,
        target_price=target,
    )


def test_gap_through_the_stop_fills_at_the_open():
    gapped = Bar(START, 17.0, 17.5, 16.5, 17.2, 1)
    assert _stop_pass(trade(), gapped) == 17.0


def test_stop_touched_intraday_fills_at_the_stop():
    dipped = Bar(START, 19.5, 20.0, 17.8, 19.0, 1)
    assert _stop_pass(trade(), dipped) == 18.0


def test_target_touched_fills_at_the_target_or_a_better_open():
    assert _high_pass(trade(), Bar(START, 25.0, 26.5, 24.8, 26.0, 1)) == 26.0
    assert _high_pass(trade(), Bar(START, 27.0, 27.5, 26.8, 27.2, 1)) == 27.0


def test_a_full_replay_produces_a_coherent_result(tmp_path):
    # Two years of a steady uptrend with a volume surge every 25 days.
    days = 2 * 252 + 300
    closes = [20 * (1.0015 ** i) for i in range(days)]
    bars = {
        symbol: [
            Bar(START + timedelta(days=i), c, c * 1.01, c * 0.99, c,
                6_000_000 if i % 25 == 0 else 2_000_000)
            for i, c in enumerate(closes)
        ]
        for symbol in ("RIVN", "KULR", BENCHMARK)
    }
    result = run_backtest(make_profile(), bars, 2, tmp_path)

    assert result.start < result.end
    assert len(result.equity_curve) == 2 * 252
    assert result.benchmark_return_pct == pytest.approx(
        (closes[-1] / closes[-2 * 252] - 1) * 100, rel=1e-6
    )
    assert result.max_drawdown_pct >= 0
    assert result.trades == sum(row["trades"] for row in result.by_setup)

"""Trailing stop and let-winners-run, through the real risk -> execution -> monitor path.

Position used throughout: long RIVN at 20.00, stop 18.00 (R = 2.00), target 26.00.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_account, make_candidate, make_profile, make_stats

from tradesys.agents.execution import ExecutionAgent
from tradesys.agents.monitor import PositionMonitor
from tradesys.agents.reflection import ReflectionAgent
from tradesys.agents.risk_manager import RiskManager
from tradesys.brokers.paper import PaperBroker
from tradesys.core.enums import ExitReason
from tradesys.core.errors import ConfigError
from tradesys.core.models import Bar
from tradesys.core.tenancy import TenantContext
from tradesys.memory.store import LocalVectorMemory
from tradesys.persistence.journal import Journal


def bars_from(closes: list[float]) -> list[Bar]:
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return [
        Bar(start + timedelta(days=i), c, c, c, c, 1_000_000) for i, c in enumerate(closes)
    ]


RISING = bars_from([18 + 0.5 * i for i in range(20)])
FALLING = bars_from([30 - 0.5 * i for i in range(20)])


class Market:
    def __init__(self, price: float, bars: list[Bar] = RISING) -> None:
        self.price = price
        self.bars = bars

    def history(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        return self.bars[-limit:]

    def last_price(self, symbol: str) -> float:
        return self.price


class RecordingBroker(PaperBroker):
    def __init__(self, *args, fail: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stop_updates: list[tuple[str, float]] = []
        self.fail = fail

    def update_stop(self, symbol: str, stop_price: float) -> None:
        if self.fail:
            raise RuntimeError("broker unreachable")
        self.stop_updates.append((symbol, stop_price))


class Stack:
    def __init__(self, tmp_path, clock, *, let_winners_run=False, fail_broker=False, bars=RISING):
        base = make_profile()
        self.profile = replace(
            base,
            risk=replace(base.risk, trailing_stop=True, let_winners_run=let_winners_run),
        )
        self.tenant = TenantContext.create(self.profile.client_id, tmp_path)
        self.journal = Journal(self.tenant)
        self.market = Market(20.0, bars)
        self.broker = RecordingBroker(self.tenant, self.market, fail=fail_broker)
        self.risk = RiskManager(self.tenant, self.profile, self.journal, clock)
        self.execution = ExecutionAgent(
            self.tenant, self.profile, self.broker, self.journal, clock=clock
        )
        reflection = ReflectionAgent(
            self.tenant, self.profile, LocalVectorMemory(self.tenant), self.journal, clock=clock
        )
        self.monitor = PositionMonitor(
            self.tenant, self.profile, self.market, self.broker, self.journal,
            self.execution, reflection, clock,
        )

    def open_position(self) -> int:
        decision = self.risk.evaluate(make_candidate(), make_account(), [], make_stats())
        assert decision.approved
        self.last_order = decision.order
        return self.execution.execute(decision).trade_id

    def at(self, price: float):
        self.market.price = price
        return self.monitor.check_once()

    def trade(self, trade_id: int):
        return self.journal.get_trade(trade_id)


def test_stop_stays_put_until_one_r_of_profit(tmp_path, clock):
    stack = Stack(tmp_path, clock)
    trade_id = stack.open_position()
    stack.at(21.0)
    assert stack.trade(trade_id).stop_price == pytest.approx(18.0)


def test_stop_moves_to_breakeven_at_one_r_then_trails_the_peak(tmp_path, clock):
    stack = Stack(tmp_path, clock)
    trade_id = stack.open_position()

    stack.at(22.0)
    assert stack.trade(trade_id).stop_price == pytest.approx(20.0)

    stack.at(25.0)
    assert stack.trade(trade_id).stop_price == pytest.approx(23.0)
    assert stack.broker.stop_updates[-1] == ("RIVN", pytest.approx(23.0))


def test_stop_never_moves_down(tmp_path, clock):
    stack = Stack(tmp_path, clock)
    trade_id = stack.open_position()
    stack.at(25.0)
    stack.at(24.0)
    assert stack.trade(trade_id).stop_price == pytest.approx(23.0)
    assert stack.journal.trail_stop(trade_id, 10.0, 10.0).stop_price == pytest.approx(23.0)


def test_trailed_stop_exits_with_a_locked_in_gain_and_true_r_multiple(tmp_path, clock):
    stack = Stack(tmp_path, clock)
    stack.open_position()
    stack.at(25.0)

    report = stack.at(22.9)

    closed = report.closed[0]
    assert closed.reason is ExitReason.STOP_LOSS
    assert closed.trade.realized_pnl > 0
    # Measured against the entry stop (R = 2.00), not the trailed one.
    assert closed.trade.r_multiple() == pytest.approx((22.9 - 20.0) / 2.0)


def test_target_is_taken_when_winners_are_not_allowed_to_run(tmp_path, clock):
    stack = Stack(tmp_path, clock)
    stack.open_position()
    assert stack.at(27.0).closed[0].reason is ExitReason.TAKE_PROFIT


def test_winner_is_held_past_target_while_the_trend_confirms(tmp_path, clock):
    stack = Stack(tmp_path, clock, let_winners_run=True, bars=RISING)
    trade_id = stack.open_position()

    report = stack.at(27.0)

    assert report.closed == []
    trade = stack.trade(trade_id)
    assert trade.target_price is None
    # Trail (27 - 2 = 25) beats half the target gain (23); either way, a locked gain.
    assert trade.stop_price == pytest.approx(25.0)
    assert any("holding" in line for line in report.adjusted)


def test_winner_is_sold_at_target_when_the_trend_fades(tmp_path, clock):
    stack = Stack(tmp_path, clock, let_winners_run=True, bars=FALLING)
    stack.open_position()
    assert stack.at(27.0).closed[0].reason is ExitReason.TAKE_PROFIT


def test_letting_winners_run_sends_no_take_profit_leg(tmp_path, clock):
    stack = Stack(tmp_path, clock, let_winners_run=True)
    trade_id = stack.open_position()
    assert stack.last_order.take_profit is None
    assert stack.last_order.stop_loss == pytest.approx(18.0)
    assert stack.trade(trade_id).target_price == pytest.approx(26.0)


def test_broker_failure_keeps_the_raised_stop_and_reports_it(tmp_path, clock):
    stack = Stack(tmp_path, clock, fail_broker=True)
    trade_id = stack.open_position()

    report = stack.at(25.0)

    assert stack.trade(trade_id).stop_price == pytest.approx(23.0)
    assert any("broker stop not moved" in e for e in report.errors)


def test_time_stop_spares_a_winner_whose_profit_is_locked(tmp_path, clock):
    stack = Stack(tmp_path, clock)
    stack.monitor._max_holding_days = 10
    trade_id = stack.open_position()
    stack.at(25.0)

    clock.advance(11 * 86400)
    report = stack.at(24.5)

    assert report.closed == []
    assert stack.trade(trade_id).stop_price == pytest.approx(23.0)


def test_time_stop_still_closes_a_trade_that_went_nowhere(tmp_path, clock):
    stack = Stack(tmp_path, clock)
    stack.monitor._max_holding_days = 10
    stack.open_position()

    clock.advance(11 * 86400)
    assert stack.at(20.5).closed[0].reason is ExitReason.TIME_STOP


def test_letting_winners_run_requires_a_trailing_stop():
    base = make_profile()
    with pytest.raises(ConfigError):
        replace(base.risk, let_winners_run=True, trailing_stop=False)

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from test_position_monitor import Stack

from tradesys.agents.monitor import PositionMonitor, read_trims
from tradesys.core.enums import ExitReason
from tradesys.data.earnings import EarningsLookup


class Calendar:
    def __init__(self, days_ahead: int | None) -> None:
        self.days_ahead = days_ahead

    def lookup(self, symbol: str, on_or_after: date) -> EarningsLookup:
        if self.days_ahead is None:
            return EarningsLookup.none_scheduled()
        return EarningsLookup.scheduled(on_or_after + timedelta(days=self.days_ahead))


def trimming(stack: Stack, days_ahead: int | None, trim_days: int = 3) -> PositionMonitor:
    profile = replace(stack.profile, risk=replace(stack.profile.risk, earnings_trim_days=trim_days))
    return PositionMonitor(
        stack.tenant, profile, stack.market, stack.broker, stack.journal,
        stack.execution, stack.reflection, stack.clock, earnings=Calendar(days_ahead),
    )


def test_half_is_sold_days_before_a_report_and_only_once(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=21.0)
    trade_id = stack.open_position()
    before = stack.journal.get_trade(trade_id).qty
    monitor = trimming(stack, days_ahead=2)

    report = monitor.check_once()

    [closed] = report.closed
    assert closed.reason is ExitReason.EARNINGS_TRIM
    assert closed.trade.qty == before // 2
    assert closed.trade.realized_pnl == (21.0 - 20.0) * (before // 2)
    remaining = stack.journal.get_trade(trade_id)
    assert remaining.qty == before - before // 2 and remaining.closed_at is None
    assert {p.symbol: p.qty for p in stack.broker.positions()}["RIVN"] == remaining.qty
    report_day = (clock.now().date() + timedelta(days=2)).isoformat()
    assert read_trims(stack.tenant.data_dir) == {str(trade_id): report_day}

    assert monitor.check_once().closed == []
    assert stack.journal.get_trade(trade_id).qty == remaining.qty


def test_a_report_further_away_or_none_or_the_rule_off_leaves_the_position(tmp_path, clock):
    for days_ahead, trim_days in ((10, 3), (None, 3), (1, 0)):
        stack = Stack(tmp_path / f"{days_ahead}-{trim_days}", clock, price=21.0)
        trade_id = stack.open_position()
        before = stack.journal.get_trade(trade_id).qty

        assert trimming(stack, days_ahead, trim_days).check_once().closed == []
        assert stack.journal.get_trade(trade_id).qty == before

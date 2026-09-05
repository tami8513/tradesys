from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import make_account, make_candidate, make_profile, make_stats

from tradesys.agents.execution import ExecutionAgent
from tradesys.agents.monitor import PositionMonitor
from tradesys.agents.reflection import ReflectionAgent
from tradesys.agents.risk_manager import RiskManager
from tradesys.brokers.paper import PaperBroker
from tradesys.core.enums import ExitReason, MemoryKind
from tradesys.core.models import Bar
from tradesys.core.tenancy import TenantContext
from tradesys.data.market_data import SyntheticMarketData
from tradesys.memory.store import LocalVectorMemory
from tradesys.persistence.journal import Journal


class ScriptedMarketData:
    """Bars come from the synthetic provider; the live price is whatever we set."""

    def __init__(self, price: float) -> None:
        self._inner = SyntheticMarketData()
        self.price = price

    def history(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        return self._inner.history(symbol, timeframe=timeframe, limit=limit)

    def last_price(self, symbol: str) -> float:
        return self.price


class Stack:
    def __init__(self, tmp_path, clock, *, price: float, max_holding_days=None):
        self.profile = make_profile()
        self.tenant = TenantContext.create(self.profile.client_id, tmp_path)
        self.journal = Journal(self.tenant)
        self.memory = LocalVectorMemory(self.tenant)
        self.market = ScriptedMarketData(price)
        self.broker = PaperBroker(self.tenant, self.market)
        self.risk = RiskManager(self.tenant, self.profile, self.journal, clock)
        self.execution = ExecutionAgent(
            self.tenant, self.profile, self.broker, self.journal, clock=clock
        )
        self.reflection = ReflectionAgent(
            self.tenant, self.profile, self.memory, self.journal, clock=clock
        )
        self.monitor = PositionMonitor(
            self.tenant,
            self.profile,
            self.market,
            self.broker,
            self.journal,
            self.execution,
            self.reflection,
            clock,
            max_holding_days=max_holding_days,
        )
        self.clock = clock

    def open_position(self) -> int:
        """Enter RIVN at 20.00, stop 18.00, target 26.00 through the real path."""
        decision = self.risk.evaluate(
            make_candidate(), make_account(), [], make_stats()
        )
        assert decision.approved
        outcome = self.execution.execute(decision)
        assert outcome.trade_id is not None
        return outcome.trade_id


def test_stop_loss_closes_the_position(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    trade_id = stack.open_position()

    assert stack.monitor.check_once().closed == []

    stack.market.price = 17.5
    report = stack.monitor.check_once()

    assert len(report.closed) == 1
    closed = report.closed[0]
    assert closed.reason is ExitReason.STOP_LOSS
    assert closed.trade.trade_id == trade_id
    assert closed.trade.realized_pnl == pytest.approx((17.5 - 20.0) * 250)
    assert stack.journal.open_trades() == []
    assert stack.broker.positions() == []


def test_take_profit_closes_the_position(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    stack.open_position()

    stack.market.price = 26.5
    report = stack.monitor.check_once()

    assert report.closed[0].reason is ExitReason.TAKE_PROFIT
    assert report.closed[0].trade.realized_pnl > 0


def test_reconciliation_outranks_a_stop_breach(tmp_path, clock):
    """A position already gone from the broker must not be 'sold' a second time."""
    stack = Stack(tmp_path, clock, price=20.0)
    stack.open_position()
    stack.broker.close_position("RIVN")

    clock.advance(120)
    stack.market.price = 17.5  # also below the stop
    report = stack.monitor.check_once()

    assert report.closed[0].reason is ExitReason.BROKER_CLOSED
    assert report.errors == []


def test_closing_writes_a_postmortem_into_that_tenants_memory(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    stack.open_position()
    before = stack.memory.count()

    stack.market.price = 17.5
    report = stack.monitor.check_once()

    assert stack.memory.count() > before
    lesson = report.closed[0].lessons[0]
    assert lesson.kind is MemoryKind.TRADE_POSTMORTEM
    assert lesson.metadata["exit_reason"] == ExitReason.STOP_LOSS.value
    assert "thesis invalidated" in lesson.text
    assert lesson.client_id == "tal_dahan"


def test_the_scout_can_retrieve_the_lesson_immediately(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    stack.open_position()
    stack.market.price = 17.5
    stack.monitor.check_once()

    recalled = stack.reflection.recall("RIVN breakout stop", k=5)
    assert any("Trade #" in text and "RIVN" in text for text in recalled)


def test_reflection_is_not_duplicated_by_a_later_batch_sweep(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    stack.open_position()
    stack.market.price = 17.5
    stack.monitor.check_once()
    count_after_monitor = stack.memory.count()

    assert stack.reflection.reflect() == []
    assert stack.memory.count() == count_after_monitor


def test_broker_reconciliation_closes_an_orphaned_journal_trade(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    stack.open_position()

    # Simulate a bracket order filling at the broker without the journal knowing.
    stack.broker.close_position("RIVN")
    assert stack.journal.open_trades() != []

    clock.advance(120)  # past the reconciliation grace period
    report = stack.monitor.check_once()

    assert report.closed[0].reason is ExitReason.BROKER_CLOSED
    assert stack.journal.open_trades() == []


def test_grace_period_prevents_reconciling_a_just_opened_trade(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    stack.open_position()
    stack.broker.close_position("RIVN")

    # Within the grace window the monitor leaves the trade alone.
    assert stack.monitor.check_once().closed == []


def test_time_stop_closes_a_stalled_thesis(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0, max_holding_days=5)
    stack.open_position()

    assert stack.monitor.check_once().closed == []
    clock.advance(timedelta(days=6).total_seconds())
    report = stack.monitor.check_once()

    assert report.closed[0].reason is ExitReason.TIME_STOP
    assert "thesis stalled" in report.closed[0].lessons[0].text


def test_manual_close_still_books_pnl_and_writes_a_lesson(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    stack.open_position()

    report = stack.monitor.close_manually("RIVN", "taking risk off before the print")

    assert report.closed[0].reason is ExitReason.MANUAL
    assert report.closed[0].lessons
    assert stack.journal.open_trades() == []


def test_a_data_outage_does_not_close_positions(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    stack.open_position()

    def broken(symbol: str) -> float:
        raise RuntimeError("data feed down")

    stack.market.last_price = broken
    report = stack.monitor.check_once()

    assert report.closed == []
    assert report.errors
    assert stack.journal.open_trades() != []


def test_exits_work_while_the_tenant_is_halted(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    stack.open_position()
    stack.journal.halt("drawdown breach")

    stack.market.price = 17.5
    report = stack.monitor.check_once()

    assert report.closed[0].reason is ExitReason.STOP_LOSS

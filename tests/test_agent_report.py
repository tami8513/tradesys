from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradesys.core.enums import SetupType, Side
from tradesys.core.models import TradeRecord
from tradesys.orchestration.agent_report import ReportInput, build_report

T0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def trade(i: int, pnl: float, setup=SetupType.BREAKOUT, reason="stop_loss") -> TradeRecord:
    # entry 20, initial stop 18 (R = 2), 100 shares: pnl / 200 = R multiple
    return TradeRecord(
        trade_id=i,
        client_id="tal_dahan",
        symbol="RIVN",
        setup=setup,
        side=Side.BUY,
        qty=100,
        entry_price=20.0,
        stop_price=18.0,
        opened_at=T0 + timedelta(days=i),
        closed_at=T0 + timedelta(days=i, hours=30),
        exit_price=20.0 + pnl / 100,
        realized_pnl=pnl,
        initial_stop=18.0,
        exit_reason=reason,
    )


def report(trades=(), events=(), statuses=(), directives=0, memory=0):
    cards = build_report(ReportInput(list(trades), list(events), list(statuses), directives, memory))
    return {c.key: c for c in cards}


def event(agent, kind, symbol="RIVN", detail=""):
    return {"agent": agent, "kind": kind, "symbol": symbol, "detail": detail, "created_at": ""}


def test_no_history_means_no_grade_anywhere():
    cards = report()
    assert all(c.status == "no_data" for c in cards.values())


def test_losing_setup_is_flagged_with_a_concrete_recommendation():
    trades = [trade(i, -150, SetupType.BREAKOUT) for i in range(5)]
    cards = report(trades)
    assert cards["scout"].status == "bad"
    assert any("פריצה" in f and "מפסידה" in f for f in cards["scout"].findings)


def test_strong_setup_is_called_out():
    trades = [trade(i, 300, SetupType.PULLBACK, "take_profit") for i in range(5)]
    cards = report(trades)
    assert cards["scout"].status == "good"
    assert any("החזקה" in f for f in cards["scout"].findings)


def test_dominant_rejection_reason_is_grouped_despite_different_numbers():
    events = [
        event("risk", "rejected", detail="qty 120 exceeds liquidity cap 80"),
        event("risk", "rejected", detail="qty 300 exceeds liquidity cap 90"),
        event("risk", "rejected", detail="earnings within 3 days"),
    ]
    cards = report(events=events)
    assert any("2 מתוך 3" in f for f in cards["risk"].findings)


def test_low_fill_rate_grades_execution_bad():
    cards = report(statuses=["filled", "canceled", "canceled", "rejected"])
    assert cards["execution"].status == "bad"


def test_losses_bigger_than_planned_point_at_gaps():
    trades = [trade(i, -300) for i in range(5)]   # -1.5R each
    cards = report(trades)
    assert cards["monitor"].status == "bad"
    assert any("פערי מחיר" in f for f in cards["monitor"].findings)


def test_trailing_stop_exit_in_profit_is_counted_as_locked_gain():
    cards = report([trade(1, 250, reason="stop_loss")])
    assert ("סטופ נגרר שנעל רווח", "1") in cards["monitor"].kpis


def test_efficiency_withholds_a_grade_until_enough_trades():
    cards = report([trade(i, 100) for i in range(4)])
    assert cards["efficiency"].status == "no_data"


def test_efficiency_grades_profit_factor_with_enough_trades():
    trades = [trade(i, 300) for i in range(6)] + [trade(10 + i, -100) for i in range(4)]
    cards = report(trades)
    assert cards["efficiency"].status == "good"
    assert ("רצף הפסדים ארוך ביותר", "4") in cards["efficiency"].kpis


def test_head_agent_surfaces_the_worst_problems_first():
    trades = [trade(i, -300) for i in range(10)]
    cards = report(trades)
    assert cards["head"].status == "bad"
    assert cards["head"].findings
    assert len(cards["head"].findings) <= 3


def test_a_full_cycle_writes_the_agent_event_log(tmp_path):
    from pathlib import Path

    from tradesys.core.config import Settings
    from tradesys.data.earnings import NullEarningsCalendar
    from tradesys.data.market_data import SyntheticMarketData
    from tradesys.orchestration.engine import TradingSystem

    root = Path(__file__).resolve().parent.parent
    system = TradingSystem(
        Settings(clients_dir=root / "clients", data_dir=tmp_path),
        market_data=SyntheticMarketData(),
        earnings=NullEarningsCalendar(),
    )
    session = system.for_client("tal_dahan")
    try:
        cycle = session.run_cycle()
        logged = session.journal.agent_events(T0 - timedelta(days=3650))
    finally:
        session.close()

    scout_events = [e for e in logged if e["agent"] == "scout"]
    risk_events = [e for e in logged if e["agent"] == "risk"]
    assert len(scout_events) == cycle.candidates
    assert len(risk_events) == cycle.approvals + len(cycle.rejections) + int(cycle.halted)

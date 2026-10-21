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
    assert any("החזקה" in f for f in cards["scout"].strengths)


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
    assert len(cards["head"].findings) <= 5


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
    session = system.for_client("demo_showcase")
    try:
        cycle = session.run_cycle()
        logged = session.journal.agent_events(T0 - timedelta(days=3650))
    finally:
        session.close()

    scout_events = [e for e in logged if e["agent"] == "scout"]
    risk_events = [e for e in logged if e["agent"] == "risk"]
    assert len(scout_events) == cycle.candidates
    assert len(risk_events) == cycle.approvals + len(cycle.rejections) + int(cycle.halted)


def open_position(qty=100, entry=20.0, stop=18.0) -> TradeRecord:
    return TradeRecord(
        trade_id=99, client_id="tal_dahan", symbol="RIVN", setup=SetupType.BREAKOUT,
        side=Side.BUY, qty=qty, entry_price=entry, stop_price=stop, opened_at=T0,
        initial_stop=stop,
    )


def stress_card(open_trades=(), r_history=(), equity=100_000.0, **risk_overrides):
    from dataclasses import replace

    from conftest import make_profile
    from tradesys.orchestration.agent_report import Book

    risk = replace(make_profile().risk, capital_floor_pct=90.0, **risk_overrides)
    book = Book("tal_dahan", equity, list(open_trades), risk, list(r_history))
    cards = build_report(ReportInput([], [], [], 0, 0, [book]))
    return {c.key: c for c in cards}["stress"]


def test_gap_through_stops_beyond_the_daily_limit_is_flagged():
    # 1000 shares, stop 18 gapped 10% to 16.20: (20 - 16.20) * 1000 = 3.8% of 100k
    card = stress_card([open_position(qty=1000)])
    assert card.status == "watch"
    assert ("פער 10% מעבר לסטופים", "-3.80%") in card.kpis


def test_crash_through_the_floor_is_the_worst_grade():
    # 6000 shares x 20 x 30% = 36k = 36% loss, floor allows 10%
    card = stress_card([open_position(qty=6000)])
    assert card.status == "bad"
    assert any("רצפת הקרן" in f for f in card.findings)


def test_losing_streak_to_halt_follows_the_limits():
    card = stress_card(max_drawdown_pct=2.0, risk_per_trade_pct=0.5, max_daily_loss_pct=1.5)
    assert ("הפסדים רצופים עד עצירה", "4") in card.kpis


def test_simulation_is_deterministic_and_flags_a_fragile_history():
    losing = [-1.0] * 8 + [0.5] * 4
    first = stress_card(r_history=losing)
    second = stress_card(r_history=losing)
    assert first.kpis == second.kpis
    assert first.status == "bad"


def test_offense_card_reports_how_the_budget_was_spread():
    events = [
        event("offense", "sized", detail="1.00|pullback"),
        event("offense", "sized", detail="0.75|breakout"),
        event("offense", "sized", detail="0.75|breakout"),
    ]
    cards = report(events=events)
    assert ("גודל מלא (מוכח)", "1") in cards["offense"].kpis
    assert ("75% (לא מוכח עדיין)", "2") in cards["offense"].kpis


def test_ideas_are_repriced_at_the_live_trade_before_sizing(tmp_path):
    """Full-tape bars lag 15 minutes on the free plan; a limit at that stale close
    sat under a rising price and was cancelled unfilled."""
    from pathlib import Path

    from conftest import make_candidate
    from tradesys.core.config import Settings
    from tradesys.data.market_data import SyntheticMarketData
    from tradesys.orchestration.engine import TradingSystem

    class LivePrint(SyntheticMarketData):
        def last_price(self, symbol):
            return 21.5

    root = Path(__file__).resolve().parent.parent
    system = TradingSystem(Settings(clients_dir=root / "clients", data_dir=tmp_path),
                           market_data=LivePrint())
    session = system.for_client("demo_showcase")
    try:
        repriced = session._at_live_price(make_candidate(client_id="demo_showcase"))
    finally:
        session.close()
    assert repriced.entry == 21.5
    assert repriced.stop == 18.0 and repriced.target == 26.0


def test_the_historical_test_agent_reads_the_latest_replay():
    replay = {
        "total_return_pct": 16.5, "benchmark_return_pct": 76.8,
        "by_setup": [
            {"name": "trend_continuation", "trades": 127, "win_rate": 0.54, "avg_r": 0.27, "pnl": 20121.7},
            {"name": "pullback", "trades": 37, "win_rate": 0.46, "avg_r": -0.07, "pnl": -2573.6},
            {"name": "breakout", "trades": 2, "win_rate": 0.0, "avg_r": -1.0, "pnl": -1700.6},
        ],
        "by_symbol": [
            {"name": "LCID", "trades": 9, "win_rate": 0.33, "avg_r": -0.55, "pnl": -4450.5},
            {"name": "PLTR", "trades": 6, "win_rate": 0.83, "avg_r": 1.2, "pnl": 7265.0},
        ],
    }
    cards = {c.key: c for c in build_report(ReportInput([], [], [], 0, 0, replays=[("tal_dahan", replay)]))}
    card = cards["replay"]

    assert card.status == "watch"
    assert ("tal_dahan: הרובוט מול המדד", "+16.5% / +76.8%") in card.kpis
    assert any("תיקון במגמה הפסידה $2,574 ב-37 עסקאות" in f and "בלעדיה" in f for f in card.findings)
    assert not any("פריצה" in f for f in card.findings)          # 2 trades are noise
    assert any("LCID הפסידה $4,450" in f for f in card.findings)
    assert any("רובוט המדדים" in f for f in card.findings)


def test_without_a_replay_the_agent_has_no_grade():
    assert {c.key: c for c in build_report(ReportInput([], [], [], 0, 0))}["replay"].status == "no_data"

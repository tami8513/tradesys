from __future__ import annotations

from datetime import datetime, timezone

from tradesys.core.enums import SetupType, Side
from tradesys.core.models import TradeRecord
from tradesys.orchestration.brief import daily_brief

T0 = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)


def ev(agent, kind, symbol="RIVN", detail=""):
    return {"agent": agent, "kind": kind, "symbol": symbol, "detail": detail, "created_at": ""}


def trade(symbol="RIVN", stop=18.0, pnl=None, reason="", closed=False) -> TradeRecord:
    return TradeRecord(
        trade_id=1, client_id="tal_dahan", symbol=symbol, setup=SetupType.BREAKOUT,
        side=Side.BUY, qty=100, entry_price=20.0, stop_price=stop, opened_at=T0,
        initial_stop=18.0, realized_pnl=pnl, exit_reason=reason,
        closed_at=T0 if closed else None,
    )


def test_quiet_day_says_so():
    brief = daily_brief([], [], [], None, 100_000.0)
    assert brief.headline.startswith("יום שקט")
    assert brief.lines == []


def test_a_full_day_reads_like_a_manager_report():
    events = [
        ev("scout", "candidate", detail="breakout|"),
        ev("risk", "approved"),
        ev("execution", "filled"),
        ev("risk", "rejected", symbol="SPCE", detail="SPCE reports in 2d (2026-10-21), inside"),
    ]
    closed = [trade("KULR", pnl=360.0, reason="take_profit", closed=True)]
    open_ = [trade("RIVN", stop=20.5)]
    brief = daily_brief(events, closed, open_, 360.0, 100_360.0)

    assert brief.headline == "היום: +$360 (+0.36%)"
    assert "נכנסנו ל-RIVN (פריצה)" in brief.lines
    assert "יצאנו מ-KULR ביעד: +$360 (+1.80R)" in brief.lines
    assert any("RIVN כבר מעל מחיר הכניסה" in line for line in brief.lines)
    assert any("חסם 1 רעיונות" in line and "דוח" in line for line in brief.lines)


def test_trailing_stop_exit_is_named_as_a_locked_gain():
    closed = [trade(pnl=250.0, reason="stop_loss", closed=True)]
    brief = daily_brief([ev("monitor", "exit")], closed, [], 250.0, 100_250.0)
    assert any("סטופ נגרר שנעל רווח" in line for line in brief.lines)


def test_worst_case_from_open_stops_is_stated():
    brief = daily_brief([ev("risk", "approved")], [], [trade(stop=18.0)], 0.0, 100_000.0)
    assert any("ההפסד המרבי מכאן הוא -$200" in line for line in brief.lines)

from __future__ import annotations

import pytest

from tradesys.orchestration.why import explain_rationale, explain_reason, timeline


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("reward:risk 1.40 below minimum 2.00", "הרווח הצפוי קטן מדי ביחס לסיכון (1.40 מול מינימום 2.00)"),
        ("5 open positions at limit 5", "כבר יש 5 פוזיציות פתוחות, וזו המגבלה (5)"),
        ("3 trades today at limit 3", "כבר נפתחו 3 עסקאות היום, וזו המגבלה (3)"),
        ("RIVN reports in 2d (2026-11-04), inside the 3d earnings window — an ATR stop "
         "cannot hold through a gap", "RIVN מפרסמת דוח בעוד 2 ימים. סטופ לא מגן מפני קפיצת מחיר של דוח"),
        ("breakout blocked in a risk-off market: SPY 480.00 vs 200-bar average 500.00",
         "השוק במגמת ירידה (מתחת לממוצע 200), ופריצות חסומות במצב כזה"),
        ("price 0.80 below min_price 1.00", "המחיר (0.80) נמוך מהמינימום המותר (1.00)"),
        ("drawdown 2.10% >= limit 2.00% (peak 100000.00, equity 97900.00)",
         "הירידה מהשיא (2.10%) הגיעה למגבלה (2.00%), והמסחר נעצר"),
    ],
)
def test_risk_reasons_read_as_plain_hebrew(reason, expected):
    assert explain_reason(reason) == expected


def test_an_unknown_reason_is_shown_as_recorded_not_guessed():
    assert explain_reason("something new happened") == "something new happened"


def test_entry_rationale_is_translated():
    text = explain_rationale("closed 12.40 above the 20-bar high 12.10 on 2.3x volume")
    assert text == "נסגרה ב-12.40, מעל השיא של 20 הימים האחרונים (12.10), במחזור פי 2.3 מהרגיל"


def ev(agent, kind, symbol="RIVN", detail="", at="2026-10-19T14:30:00"):
    return {"agent": agent, "kind": kind, "symbol": symbol, "detail": detail, "created_at": at}


def test_timeline_tells_the_story_newest_first():
    events = [
        ev("scout", "candidate", detail="breakout|RSI 25 with an up-close reversal bar"),
        ev("risk", "approved", detail="breakout"),
        ev("execution", "filled"),
        ev("monitor", "stop_raised", detail="RIVN stop 18.00 -> 20.00", at="2026-10-20T15:00:00"),
        ev("monitor", "exit", detail="stop_loss|1.45", at="2026-10-21T15:00:00"),
    ]
    entries = timeline(events)
    assert entries[0].text == "סוכן היציאה סגר את העסקה בסטופ נגרר שנעל רווח, תוצאה +1.45R"
    assert entries[0].tone == "positive"
    assert entries[1].text == "הרווח מתקדם: הסטופ עלה מ-18.00 ל-20.00"
    assert entries[-1].text.startswith("סוכן הכניסה זיהה פריצה: מכירת יתר (RSI 25)")
    assert entries[-1].when == "2026-10-19 14:30"


def test_times_are_shown_in_israel_time():
    [entry] = timeline([ev("risk", "approved", at="2026-10-19T13:30:00+00:00")])
    assert entry.when == "2026-10-19 16:30"


def test_rejection_lists_every_reason():
    [entry] = timeline([
        ev("risk", "rejected", detail="5 open positions at limit 5 || already holding RIVN; pyramiding is disabled"),
    ])
    assert entry.tone == "negative"
    assert "כבר יש 5 פוזיציות פתוחות" in entry.text and "כבר מחזיקים ב-RIVN" in entry.text


def test_timeline_can_follow_one_stock():
    events = [ev("risk", "approved", symbol="RIVN"), ev("risk", "approved", symbol="KULR")]
    assert [e.symbol for e in timeline(events, symbol="KULR")] == ["KULR"]


def test_latest_by_agent_keeps_only_each_agents_newest_line():
    from tradesys.orchestration.why import latest_by_agent

    events = [
        ev("risk", "approved", symbol="RIVN", at="2026-10-19T14:00:00"),
        ev("risk", "rejected", symbol="KULR", detail="3 trades today at limit 3", at="2026-10-19T15:00:00"),
        ev("scout", "candidate", symbol="KULR", detail="breakout|", at="2026-10-19T15:00:00"),
    ]
    latest = latest_by_agent(events)
    assert latest["risk"].symbol == "KULR" and latest["risk"].tone == "negative"
    assert set(latest) == {"risk", "scout"}

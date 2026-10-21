from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from pytest import approx

from tradesys.core.models import Bar
from tradesys.orchestration.agent_report import Ops, ReportInput, build_report
from tradesys.orchestration.track_record import (
    Call,
    Outcome,
    log_analysis,
    outcomes,
    read_analyses,
    scout_calls,
)

TODAY = date(2026, 11, 20)


def series(start: date, closes: list[float]) -> list[Bar]:
    return [Bar(datetime.combine(start + timedelta(days=i), datetime.min.time(), timezone.utc), c, c, c, c, 1)
            for i, c in enumerate(closes)]


def test_a_call_is_priced_again_against_the_index_once_a_week_old():
    bars = {"SPY": series(date(2026, 11, 1), [100] * 10 + [110] * 10),
            "NVDA": series(date(2026, 11, 1), [200] * 20)}
    old = Call(date(2026, 11, 2), "NVDA", "A", 180.0)
    fresh = Call(date(2026, 11, 18), "NVDA", "A", 180.0)

    [result] = outcomes([old, fresh], bars, TODAY)

    assert result.ret == approx(200 / 180 * 100 - 100)
    assert result.bench == approx(10.0)
    assert result.beat


def test_analyses_are_logged_once_per_stock_per_day(tmp_path):
    log_analysis(tmp_path, TODAY, "NVDA", "go", 200.0)
    log_analysis(tmp_path, TODAY, "NVDA", "no", 201.0)
    log_analysis(tmp_path, TODAY, "MSFT", "careful", 500.0)

    assert [(c.symbol, c.label) for c in read_analyses(tmp_path)] == [("NVDA", "go"), ("MSFT", "careful")]


def test_the_scouts_shortlists_are_read_back_with_their_grades(tmp_path):
    (tmp_path / "scout_2026-11-03.json").write_text(json.dumps({
        "made_at": "2026-11-02T21:00:00+00:00",
        "reports": [{"symbol": "AMD", "grade": "A", "price": 240.0}, {"symbol": "HTZ", "grade": "S", "price": 1.9}],
    }), encoding="utf-8")

    calls = scout_calls(tmp_path, TODAY)

    assert [(c.day, c.symbol, c.label) for c in calls] == [
        (date(2026, 11, 2), "AMD", "A"), (date(2026, 11, 2), "HTZ", "S")]


def outcome(label: str, ret: float, bench: float = 2.0) -> Outcome:
    return Outcome(Call(date(2026, 11, 2), "X", label, 10.0), ret, bench)


def cards(**data):
    return {c.key: c for c in build_report(ReportInput([], [], [], 0, 0, **data))}


def test_the_scout_is_graded_on_whether_its_a_names_beat_the_index():
    winning = cards(scout_outcomes=[outcome("A", 8.0)] * 6)["talent_scout"]
    losing = cards(scout_outcomes=[outcome("A", -3.0)] * 6)["talent_scout"]
    early = cards(scout_outcomes=[outcome("A", 8.0)] * 2)["talent_scout"]

    assert winning.status == "good" and winning.strengths
    assert losing.status == "bad" and any("פיגרו" in f for f in losing.findings)
    assert early.status == "no_data" and "עוד מוקדם" in early.verdict


def test_the_analyst_is_graded_on_whether_yes_beat_not_now():
    data = [outcome("go", 6.0)] * 5 + [outcome("no", -2.0)] * 5
    card = cards(analysis_outcomes=data)["analyst"]

    assert card.status == "good"
    assert "הקדימו" in card.verdict


def test_a_disconnected_phone_is_the_alerts_agents_problem():
    off = cards(ops=[Ops("tal_dahan", False, 2, 0, 0)])["alerts"]
    on = cards(ops=[Ops("tal_dahan", True, 0, 3, 2)])["alerts"]

    assert off.status == "bad" and "לא מחובר" in off.verdict
    assert on.status == "good" and ("שאלות בוקר / נענו", "3 / 2") in on.kpis


def test_every_agent_has_a_one_line_verdict():
    assert all(card.verdict for key, card in cards().items() if key != "head")

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from tradesys.agents.chart_analyst import Evidence, _levels, analyze, evidence_text
from tradesys.core.models import Bar

START = datetime(2016, 1, 4, tzinfo=timezone.utc)


def bars(closes: list[float], volumes: list[float] | None = None) -> list[Bar]:
    volumes = volumes or [1_000_000.0] * len(closes)
    return [
        Bar(START + timedelta(days=i), c, c * 1.01, c * 0.99, c, v)
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


def wave(n: int, drift: float, amplitude: float = 0.03) -> list[float]:
    """A drifting price with a gentle monthly swing, so readings change over time."""
    return [100 * (1 + drift) ** i * (1 + amplitude * math.sin(i / 7)) for i in range(n)]


def test_a_falling_chart_says_do_not_enter():
    reading = analyze("DOWN", bars(wave(400, -0.002)))

    assert reading.signal == "downtrend"
    assert reading.action == "avoid"
    assert reading.target is None
    assert "לא להיכנס" in reading.headline
    assert "להיכנס רק אחרי חזרה מעל" in reading.plan


def test_a_dip_inside_an_uptrend_is_an_entry():
    closes = [100 * 1.003**i for i in range(300)]
    closes += [closes[-1] * (1 - 0.012 * k) for k in range(1, 9)]

    reading = analyze("DIP", bars(closes))

    assert reading.signal == "pullback"
    assert reading.action == "enter"
    assert reading.stop < reading.entry_zone[0] <= reading.entry_zone[1] < reading.target
    assert reading.plan.startswith("אזור כניסה")


def test_a_new_high_on_heavy_volume_is_a_breakout():
    closes = [100 + 10 * math.sin(i / 9) + i * 0.15 for i in range(300)]
    closes.append(max(closes) * 1.02)
    volumes = [1_000_000.0] * 300 + [3_000_000.0]

    assert analyze("POP", bars(closes, volumes)).signal == "breakout"


def test_a_steep_climb_says_wait_for_a_dip():
    closes = [100 * 1.001**i for i in range(280)] + [100 * 1.001**280 * 1.01**k for k in range(1, 21)]

    reading = analyze("HOT", bars(closes))

    assert reading.signal == "stretched"
    assert reading.action == "wait"


def test_history_is_checked_and_compared_with_an_ordinary_day():
    reading = analyze("WAVE", bars(wave(1200, 0.0008)))

    assert reading.evidence.cases > 0
    assert 0 <= reading.evidence.win_rate <= 100
    assert 0 <= reading.evidence.base_win_rate <= 100


def test_too_little_history_gives_no_reading():
    assert analyze("NEW", bars(wave(150, 0.001))) is None


def test_evidence_is_explained_honestly():
    assert "אין מספיק מקרים" in evidence_text(Evidence(3, 100, 5, 55, 1))
    strong = evidence_text(Evidence(20, 75, 3.2, 58, 1.1))
    assert "20 פעמים" in strong and "75%" in strong and "יותר מהרגיל" in strong
    assert "לא עבדה" in evidence_text(Evidence(20, 57, 1.0, 58, 1.1))
    assert "פחות טוב" in evidence_text(Evidence(20, 40, -1.0, 58, 1.1))


def test_levels_below_the_price_support_it_and_above_resist_it():
    text = _levels(100, {"ממוצע 50": 105, "ממוצע 200": 90})

    assert text == "תמיכה: ממוצע 200 ב-90.00 · התנגדות: ממוצע 50 ב-105.00"

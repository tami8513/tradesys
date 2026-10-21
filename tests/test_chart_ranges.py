from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pytest import approx

from tradesys.core.models import Bar
from tradesys.data.chart_ranges import history, regular_session, weekly


def bar(ts: datetime, close: float, volume: float = 100.0) -> Bar:
    return Bar(ts, close, close + 1, close - 1, close, volume)


def test_intraday_keeps_only_regular_hours_of_the_last_sessions():
    # 13:30 UTC is 9:30 in New York during daylight saving time.
    day1 = datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)
    bars = [bar(d + timedelta(minutes=m), 1) for d in (day1, day2) for m in (0, 30, 60, 425)]

    kept = regular_session(bars, sessions=1)

    assert [b.ts for b in kept] == [day2 + timedelta(minutes=30), day2 + timedelta(minutes=60)]


def test_daily_bars_fold_into_weeks():
    monday = datetime(2026, 10, 12, tzinfo=timezone.utc)
    days = [bar(monday + timedelta(days=i), 10 + i, volume=1) for i in (0, 1, 2, 7, 8)]

    weeks = weekly(days)

    assert len(weeks) == 2
    assert (weeks[0].open, weeks[0].close, weeks[0].volume) == (10, 12, 3)
    assert (weeks[0].high, weeks[0].low) == (13, 9)
    assert weeks[1].close == 18


def test_history_reports_each_year_and_the_deepest_fall():
    points = [
        (datetime(2020, 6, 1), 100), (datetime(2020, 12, 31), 120),
        (datetime(2021, 6, 1), 60), (datetime(2021, 12, 31), 90),
        (datetime(2022, 12, 30), 135), (datetime(2023, 3, 1), 150),
    ]
    research = history([bar(ts.replace(tzinfo=timezone.utc), c) for ts, c in points])

    assert [y for y, _ in research.years] == [2020, 2021, 2022, 2023]
    assert [r for _, r in research.years] == approx([20.0, -25.0, 50.0, 150 / 135 * 100 - 100])
    assert research.total == approx(50.0)
    assert research.max_drawdown == -50.0
    assert (research.up_years, research.full_years) == (1, 2)
    assert research.first_day == "2020-06-01"

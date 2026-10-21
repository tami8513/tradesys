from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradesys.agents.chart_analyst import Evidence
from tradesys.agents.money_flow import describe, flow_points, read_flow
from tradesys.core.models import Bar

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def day(i: int, close: float, volume: float = 1e6, low: float | None = None, high: float | None = None) -> Bar:
    return Bar(START + timedelta(days=i), close, high or close * 1.01, low or close * 0.99, close, volume)


def quiet(n: int) -> list[Bar]:
    return [day(i, 100 + (i % 3) * 0.1) for i in range(n)]


def test_heavy_volume_closing_at_the_high_is_unusual_buying():
    bars = quiet(80) + [day(80, 104, volume=3e6, low=100, high=104.2)]

    flow = read_flow(bars)

    assert flow.unusual_buying and not flow.unusual_selling
    assert round(flow.relative_volume, 1) == 3.0
    assert flow.close_location > 0.9
    assert "קנייה חריגה היום: נסחרה פי 3.0" in describe(flow)[0]


def test_heavy_volume_closing_at_the_low_is_unusual_selling():
    bars = quiet(80) + [day(80, 96, volume=3e6, low=95.9, high=100)]

    flow = read_flow(bars)

    assert flow.unusual_selling and not flow.unusual_buying
    assert describe(flow)[0].startswith("מכירה חריגה היום")


def test_an_ordinary_day_is_neither_and_counts_heavy_days():
    bars = quiet(60)
    bars += [day(60 + k, 101 + k, volume=2e6) for k in range(5)]     # five heavy up days
    bars += [day(65 + k, 106, volume=5e5) for k in range(5)]         # quiet since

    flow = read_flow(bars)

    assert not flow.unusual_buying and not flow.unusual_selling
    assert flow.accumulation_days == 5 and flow.distribution_days == 0


def test_a_verdict_moves_only_on_this_stocks_own_history():
    base = dict(relative_volume=3.0, close_location=0.9, money_flow=0.2,
                accumulation_days=4, distribution_days=1, unusual_buying=True, unusual_selling=False)
    bars = quiet(80) + [day(80, 104, volume=3e6, low=100, high=104.2)]
    flow = read_flow(bars)

    worked = flow.__class__(**base, evidence=Evidence(12, 75.0, 3.0, 58.0, 1.5))
    failed = flow.__class__(**base, evidence=Evidence(12, 40.0, -1.0, 58.0, 1.5))
    thin = flow.__class__(**base, evidence=Evidence(3, 100.0, 5.0, 58.0, 1.5))

    assert flow_points(worked)[0] == 1
    assert flow_points(failed)[0] == -1
    assert flow_points(thin)[0] == 0
    assert flow_points(read_flow(quiet(80))) is None


def test_too_little_history_gives_no_reading():
    assert read_flow(quiet(30)) is None

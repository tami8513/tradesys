from __future__ import annotations

from tradesys.agents.valuation import judge, read_valuation
from tradesys.data.scouting_feeds import ScoutingFeeds


def test_forward_pe_below_todays_means_profit_is_expected_to_grow():
    value = read_valuation({"peTTM": 30.0, "forwardPE": 24.0})

    good, bad, points = judge(value)

    assert round(value.implied_growth) == 25
    assert any("יגדל בכ-25%" in s for s in good) and not bad
    assert value.summary() == "מכפיל רווח 30.0 · מכפיל עתידי 24.0"


def test_cheap_on_growth_and_on_its_own_history_earns_a_point():
    value = read_valuation({"peTTM": 20.0, "epsGrowth5Y": 40.0}, [30.0] * 12)

    good, _, points = judge(value)

    assert value.peg == 0.5 and value.pe_median == 30.0
    assert points == 1
    assert any("זולה ביחס לצמיחה" in s for s in good) and any("זולה ביחס לעבר" in s for s in good)


def test_dear_on_both_loses_a_point_and_a_loss_maker_is_flagged():
    _, bad, points = judge(read_valuation({"peTTM": 90.0, "epsGrowthTTMYoy": 10.0}, [40.0] * 12))
    assert points == -1 and any("יקרה ביחס לצמיחה" in w for w in bad)

    losing = read_valuation({"peTTM": -12.0})
    _, bad, points = judge(losing)
    assert losing.losing_money and points == -1 and "מפסידה" in losing.summary()


def test_other_vendor_spellings_and_missing_data():
    assert read_valuation({"peBasicExclExtraTTM": 18.5}).pe == 18.5
    assert read_valuation({"revenueGrowthTTMYoy": 20.0}) is None
    assert read_valuation(None) is None
    assert read_valuation({"peTTM": 20.0}, [25.0] * 3).pe_median is None   # too little history


def test_the_feed_reads_pe_and_its_quarterly_history(monkeypatch):
    payload = {
        "metric": {"peTTM": 31.2, "forwardPE": 26.0, "epsGrowth5Y": 22.0, "revenueGrowthTTMYoy": 18.0},
        "series": {"quarterly": {"peTTM": [{"period": "2026-06-30", "v": 33.0}, {"period": "2026-03-31", "v": 29.5}]}},
    }
    feeds = ScoutingFeeds("k", "s", "finnhub-key", sleep=lambda _: None)
    monkeypatch.setattr(feeds, "_finnhub", lambda path, symbol, **params: payload)

    found = feeds.growth("NVDA")

    assert found["peTTM"] == 31.2 and found["forwardPE"] == 26.0
    assert found["pe_history"] == [33.0, 29.5]

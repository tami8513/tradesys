from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from tradesys.agents.talent_scout import (
    TalentScout,
    _percentiles,
    sentiment,
    load_reports,
    save_reports,
)
from tradesys.backtest import ReplayMarketData
from tradesys.core.models import Bar

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
LAST = (START + timedelta(days=239)).date()
NAMES = {
    "FAST": "Fast Growth Inc",
    "SLOW": "Slow Climb Corp",
    "DOWN": "Falling Knife Co",
    "PENNY": "Tiny Penny Ltd",
    "THIN": "Thin Volume Inc",
    "TQQQ": "ProShares UltraPro QQQ 3X",
    "ABCDW": "Some SPAC Warrant",
}


def trend(daily: float, start: float = 100.0, volume: float = 1_000_000) -> list[Bar]:
    return [
        Bar(START + timedelta(days=i), start * daily**i, start * daily**i * 1.01,
            start * daily**i * 0.99, start * daily**i, volume)
        for i in range(240)
    ]


BARS = {
    "FAST": trend(1.004),
    "SLOW": trend(1.001),
    "DOWN": trend(0.998),
    "PENNY": trend(1.004, start=1.0, volume=50_000_000),
    "THIN": trend(1.004, volume=1_000),
    "TQQQ": trend(1.004),
    "ABCDW": trend(1.004),
}


def scout(**feeds) -> TalentScout:
    market = ReplayMarketData(BARS)
    market.set_day(LAST)
    return TalentScout(market, names=NAMES.get, **feeds)


def test_only_liquid_rising_ordinary_stocks_make_the_list():
    reports = scout(movers=lambda: ["TQQQ", "ABCDW", "PENNY"]).shortlist(list(NAMES), 5.0)

    assert [r.symbol for r in reports] == ["FAST", "SLOW"]


def test_movers_widen_the_pool_beyond_the_universe():
    reports = scout(movers=lambda: ["FAST"]).shortlist(["SLOW"], 5.0)

    assert {r.symbol for r in reports} == {"FAST", "SLOW"}


def test_analysts_and_growth_lift_a_name_and_are_explained():
    def analysts(symbol):
        return {"strongBuy": 10, "buy": 5, "hold": 1, "sell": 0, "strongSell": 0} if symbol == "SLOW" else \
            {"strongBuy": 0, "buy": 0, "hold": 2, "sell": 5, "strongSell": 3}

    def growth(symbol):
        return {"revenueGrowthTTMYoy": 40.0, "epsGrowthTTMYoy": 30.0} if symbol == "SLOW" else \
            {"revenueGrowthTTMYoy": -10.0, "epsGrowthTTMYoy": -20.0}

    reports = {r.symbol: r for r in scout(analysts=analysts, growth=growth).shortlist(["FAST", "SLOW"], 5.0)}

    assert reports["SLOW"].parts["analysts"] > reports["FAST"].parts["analysts"]
    assert any("15 מתוך 16 אנליסטים" in s for s in reports["SLOW"].strengths)
    assert any("ההכנסות צומחות ב-40%" in s for s in reports["SLOW"].strengths)
    assert any("יותר אנליסטים ממליצים למכור" in w for w in reports["FAST"].weaknesses)
    assert any("הרווח למניה ירד" in w for w in reports["FAST"].weaknesses)


def test_news_tone_red_flags_and_earnings_land_in_the_report():
    def news(symbols):
        return [
            {"headline": "FAST beats estimates, raises guidance", "summary": "", "symbols": ["FAST"]},
            {"headline": "SLOW faces SEC probe", "summary": "lawsuit filed", "symbols": ["SLOW"]},
        ]

    reports = {
        r.symbol: r
        for r in scout(news=news, earnings_in_days=lambda s: 3 if s == "FAST" else None)
        .shortlist(["FAST", "SLOW"], 5.0)
    }

    assert reports["FAST"].parts["news"] > reports["SLOW"].parts["news"]
    assert any("הטון חיובי" in s for s in reports["FAST"].strengths)
    assert any("דגל אדום בחדשות: תביעה, חקירה" in w for w in reports["SLOW"].weaknesses)
    assert any("דוח רבעוני בעוד 3 ימים" in w for w in reports["FAST"].weaknesses)
    assert reports["FAST"].headlines == ["FAST beats estimates, raises guidance"]


def test_trailing_the_benchmark_is_a_weakness():
    report = scout().shortlist(["SLOW"], 500.0)[0]

    assert any("מפגרת אחרי המדד" in w for w in report.weaknesses)


def test_sentiment_counts_whole_words_only():
    assert sentiment("Company will execute its plan") == 0
    assert sentiment("Revenue beats; analysts upgrade") == 2
    assert sentiment("Guidance cut after a miss") == -2


def test_ties_share_a_rank():
    ranks = _percentiles({"A": 0, "B": 0, "C": 3})

    assert ranks == {"A": 25.0, "B": 25.0, "C": 100.0}


def test_reports_round_trip_and_the_latest_is_found(tmp_path):
    reports = scout().shortlist(["FAST"], 5.0)
    save_reports(tmp_path, date(2026, 10, 20), reports)

    made_at, loaded = load_reports(tmp_path, date(2026, 10, 22))

    assert made_at
    assert loaded == reports
    assert load_reports(tmp_path, date(2026, 11, 20)) is None


def test_red_flags_and_a_stretched_chart_cost_points():
    clean = scout().shortlist(["FAST"], 5.0)[0]
    flagged = scout(news=lambda symbols: [
        {"headline": "FAST announces stock offering", "summary": "", "symbols": ["FAST"]},
    ]).shortlist(["FAST"], 5.0)[0]

    assert flagged.score == clean.score - 10


def test_the_nightly_round_runs_once_per_trading_day(tmp_path):
    from dataclasses import replace as with_

    from tradesys.core.config import Settings
    from tradesys.orchestration.scouting import scout_due

    settings = with_(Settings.from_env(tmp_path), data_dir=tmp_path, market_data_key_id="key")
    day = date(2026, 10, 22)

    assert scout_due(settings, day)
    save_reports(tmp_path, day, [])
    assert not scout_due(settings, day)
    assert not scout_due(with_(settings, market_data_key_id=""), date(2026, 10, 23))


GOOD_NEWS = [
    {"headline": "PENNY wins record contract", "summary": "", "symbols": ["PENNY"]},
    {"headline": "PENNY shares surge after FDA approval", "summary": "", "symbols": ["PENNY"]},
]


def long_shots(news=GOOD_NEWS, days_to_cover=6.0):
    reports = scout(
        news=lambda symbols: [n for n in news if set(n["symbols"]) & set(symbols)],
        short_interest=lambda symbols: {
            s: {"days_to_cover": days_to_cover, "change_pct": 25.0} for s in symbols
        },
    ).shortlist(["FAST", "PENNY"], 5.0)
    return [r for r in reports if r.grade == "S"], reports


def test_a_cheap_stock_with_great_news_and_heavy_shorting_is_a_long_shot():
    shots, reports = long_shots()

    assert [r.symbol for r in shots] == ["PENNY"]
    assert reports[-1].symbol == "PENNY"     # after the regular names
    assert any("6.0 ימי מסחר" in s for s in shots[0].strengths)
    assert any("השורט גדל ב-25%" in s for s in shots[0].strengths)
    assert any("הימור, לא השקעה" in w for w in shots[0].weaknesses)


def test_a_long_shot_needs_both_great_news_and_heavy_shorting():
    assert long_shots(days_to_cover=2.0)[0] == []
    assert long_shots(news=GOOD_NEWS[:1])[0] == []
    flagged = [*GOOD_NEWS, {"headline": "PENNY plans stock offering", "summary": "", "symbols": ["PENNY"]}]
    assert long_shots(news=flagged)[0] == []


def test_a_product_offering_is_not_a_share_sale():
    from tradesys.agents.talent_scout import red_flags

    assert red_flags([{"headline": "Updates across its AI offering with Nvidia"}]) == []
    assert red_flags([{"headline": "Company prices $200M public offering"}]) == ["הנפקת מניות"]

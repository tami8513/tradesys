from __future__ import annotations

from tradesys.agents.chart_analyst import ChartReading, Evidence
from tradesys.agents.strategy_lab import LabReport, Plan, Result
from tradesys.orchestration.deep_analysis import CAREFUL, GO, NO, verdict


def reading(action="enter", win=75.0, base=60.0) -> ChartReading:
    return ChartReading(
        symbol="NVDA", signal="pullback", action=action, headline="נקודת כניסה טובה",
        price=100, entry_zone=(98, 100), stop=90, target=120,
        evidence=Evidence(cases=30, win_rate=win, average=3.0, base_win_rate=base, base_average=1.0),
    )


def lab(train=40.0, test=10.0, hold_train=30.0, hold_test=5.0) -> LabReport:
    def result(total):
        return Result(total, -20.0, 10, 60.0, 80.0)

    best = Plan("dips", 3.0, result(train), result(test))
    return LabReport(best, [best], result(hold_train), result(hold_test), "2021-10-21", "2025-10-21")


GOOD_NEWS = [{"headline": "NVDA beats estimates and raises guidance", "summary": "", "symbols": ["NVDA"]}]


def test_everything_lining_up_says_go_and_shows_its_reasons():
    answer = verdict(reading(), lab(), GOOD_NEWS,
                     analysts={"strongBuy": 20, "buy": 10, "hold": 5, "sell": 0, "strongSell": 0},
                     growth={"revenueGrowthTTMYoy": 60.0})

    assert answer.level == GO
    assert answer.score == 2 + 1 + 1 + 1 + 1 + 1 + 1
    assert any("לא שימשה לבחירה" in why for _, why in answer.reasons)
    assert not answer.hold_better


def test_a_falling_chart_and_a_red_flag_say_not_now():
    flagged = [{"headline": "NVDA faces SEC probe", "summary": "", "symbols": ["NVDA"]}]

    answer = verdict(reading(action="avoid"), lab(), flagged)

    assert answer.level == NO
    assert any(points == -2 and "דגל אדום" in why for points, why in answer.reasons)


def test_a_method_that_failed_on_its_unseen_year_is_marked_down():
    answer = verdict(reading(action="maybe", win=60, base=60), lab(test=-8.0), [])

    assert any(points == -1 and "הפסידה בשנה האחרונה" in why for points, why in answer.reasons)
    assert answer.level == NO


def test_when_holding_beat_the_robot_the_client_is_told():
    answer = verdict(reading(), lab(train=20.0, hold_train=80.0, hold_test=15.0), GOOD_NEWS)

    assert answer.hold_better
    assert answer.level == CAREFUL or answer.level == GO


def test_an_earnings_report_days_away_costs_a_point():
    calm = verdict(reading(), lab(), GOOD_NEWS)
    close = verdict(reading(), lab(), GOOD_NEWS, earnings_in_days=3)

    assert close.score == calm.score - 1


def test_a_red_flag_counts_only_in_a_headline():
    news = [{"headline": "NVDA beats estimates", "summary": "a broad product offering", "symbols": ["NVDA"]}]

    answer = verdict(reading(), lab(), news)

    assert not any("דגל אדום" in why for _, why in answer.reasons)


def test_money_flow_counts_only_when_this_stock_backs_it():
    from tradesys.agents.money_flow import FlowReading

    def flow(win):
        return FlowReading(3.0, 0.9, 0.2, 5, 1, True, False, Evidence(12, win, 2.0, 58.0, 1.0))

    plain = verdict(reading(), lab(), GOOD_NEWS)
    backed = verdict(reading(), lab(), GOOD_NEWS, flow=flow(75.0))
    against = verdict(reading(), lab(), GOOD_NEWS, flow=flow(40.0))

    assert backed.score == plain.score + 1
    assert against.score == plain.score - 1
    assert any("זרימת הכסף" in why for _, why in backed.reasons)


def test_valuation_shows_and_moves_the_verdict_only_at_the_extremes():
    cheap = verdict(reading(), lab(), GOOD_NEWS,
                    growth={"peTTM": 20.0, "epsGrowth5Y": 40.0, "pe_history": [30.0] * 12})
    plain = verdict(reading(), lab(), GOOD_NEWS)

    assert any(why.startswith("שווי: מכפיל רווח 20.0") for _, why in cheap.reasons)
    assert cheap.score == plain.score + 1

from __future__ import annotations

from datetime import date

from pytest import approx

from tradesys.agents.index_robot import IndexView, plan, refresh
from tradesys.clients.index_sleeve import (
    Ledger,
    book_equity,
    clean_settings,
    read_ledger,
    read_settings,
    write_ledger,
    write_settings,
)
from tradesys.core.enums import Side

TODAY = date(2026, 10, 21)
UP = IndexView("trend", death_cross=False)
DIP = IndexView("pullback", death_cross=False)
HOT = IndexView("stretched", death_cross=False)
BEAR = IndexView("downtrend", death_cross=True)


def settings(budget=30_000.0, indices=("SPY", "QQQ"), insurance=False, enabled=True):
    return clean_settings(enabled, budget, indices, insurance, acknowledged=True)


# ------------------------------------------------------------------ settings

def test_the_robot_cannot_be_on_without_the_clients_consent():
    assert not clean_settings(True, 10_000, ("SPY",), False, acknowledged=False).enabled
    assert not clean_settings(True, 10_000, ("NOPE",), False, acknowledged=True).enabled
    assert clean_settings(True, -5, ("SPY", "SPY"), False, True).budget == 0
    assert clean_settings(True, 5, ("SPY", "SPY"), False, True).indices == ("SPY",)


def test_settings_and_ledger_round_trip(tmp_path):
    chosen = settings(insurance=True)
    write_settings(tmp_path, chosen)
    assert read_settings(tmp_path) == chosen
    assert not read_settings(tmp_path / "nobody").enabled

    ledger = Ledger()
    ledger.bought("SPY", 10, 500.0, "2026-10-20")
    ledger.sold("SPY", 5, 520.0, park=True)
    write_ledger(tmp_path, ledger)
    assert read_ledger(tmp_path) == ledger


# --------------------------------------------------------------- the books

def test_the_main_robot_never_sees_the_index_robots_profit_or_loss():
    ledger = Ledger()
    ledger.bought("SPY", 100, 500.0, "2026-10-20")       # $50,000 moved in
    account = 100_000.0                                   # cash 50k + SPY 50k

    assert book_equity(account, ledger, {"SPY": 500.0}) == approx(100_000)
    # SPY falls 10%: the account loses $5,000, the main book does not.
    assert book_equity(95_000.0, ledger, {"SPY": 450.0}) == approx(100_000)

    ledger.sold("SPY", 50, 450.0, park=True)             # insurance: cash stays with the robot
    assert book_equity(95_000.0, ledger, {"SPY": 450.0}) == approx(100_000)
    assert ledger.insured == ["SPY"]

    ledger.bought("SPY", 50, 460.0, "2026-11-20")        # buys back from the parked cash
    assert ledger.parked["SPY"] == approx(22_500 - 22_500)
    assert ledger.contributed["SPY"] == approx(50_000 + 500)


def test_a_budget_cut_returns_the_money_to_the_main_book():
    ledger = Ledger()
    ledger.bought("QQQ", 10, 600.0, "2026-10-20")
    ledger.sold("QQQ", 5, 600.0, park=False)

    assert ledger.contributed["QQQ"] == approx(3_000)
    assert ledger.realized == approx(0)


# ---------------------------------------------------------------- decisions

def test_new_money_goes_in_whole_on_a_dip_and_in_thirds_otherwise():
    orders = plan(settings(), Ledger(), {"SPY": DIP, "QQQ": UP}, TODAY, can_buy=True)

    by_symbol = {o.symbol: o for o in orders}
    assert by_symbol["SPY"].dollars == approx(15_000)
    assert by_symbol["QQQ"].dollars == approx(5_000)
    assert all(o.side is Side.BUY for o in orders)


def test_thirds_are_spaced_and_a_hot_market_waits_but_not_forever():
    ledger = Ledger()
    ledger.bought("SPY", 10, 500.0, "2026-10-19")
    assert plan(settings(indices=("SPY",)), ledger, {"SPY": UP}, TODAY, can_buy=True) == []

    ledger.last_buy["SPY"] = "2026-10-10"
    assert plan(settings(indices=("SPY",)), ledger, {"SPY": HOT}, TODAY, can_buy=True) == []
    ledger.last_buy["SPY"] = "2026-09-01"
    assert plan(settings(indices=("SPY",)), ledger, {"SPY": HOT}, TODAY, can_buy=True)


def test_bear_insurance_sells_half_once_then_buys_back_after_the_trend_turns():
    chosen = settings(indices=("SPY",), insurance=True)
    ledger = Ledger()
    ledger.bought("SPY", 60, 500.0, "2026-01-01")      # the whole $30,000 budget

    sell = plan(chosen, ledger, {"SPY": BEAR}, TODAY, can_buy=True)
    assert [(o.side, o.fraction, o.park) for o in sell] == [(Side.SELL, 0.5, True)]

    ledger.sold("SPY", 30, 480.0, park=True)
    assert plan(chosen, ledger, {"SPY": BEAR}, TODAY, can_buy=True) == []

    refresh(chosen, ledger, {"SPY": DIP})
    buy = plan(chosen, ledger, {"SPY": DIP}, TODAY, can_buy=True)
    assert buy[0].side is Side.BUY and buy[0].dollars == approx(30 * 480.0)


def test_removed_indices_are_sold_and_a_halt_stops_only_buying():
    ledger = Ledger()
    ledger.bought("IWM", 10, 250.0, "2026-01-01")
    orders = plan(settings(indices=("SPY",)), ledger, {"SPY": DIP, "IWM": UP}, TODAY, can_buy=False)

    assert [(o.symbol, o.side, o.fraction) for o in orders] == [("IWM", Side.SELL, 1.0)]


def test_an_index_the_main_robot_holds_is_left_alone_and_off_means_frozen():
    views = {"SPY": DIP, "QQQ": DIP}
    orders = plan(settings(), Ledger(), views, TODAY, can_buy=True, blocked=frozenset({"SPY"}))
    assert [o.symbol for o in orders] == ["QQQ"]

    assert plan(settings(enabled=False), Ledger(), views, TODAY, can_buy=True) == []

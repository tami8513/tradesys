from __future__ import annotations

from datetime import date

from pytest import approx

from tradesys.agents.index_robot import IndexView, plan, refresh, swing_signals
from tradesys.clients.index_sleeve import Ledger, clean_settings, read_ledger, write_ledger
from tradesys.core.enums import Side

TODAY = date(2026, 10, 26)
ABOVE = IndexView("trend", death_cross=False, below_trend=False, rsi2=55, above_sma5=True)
BELOW = IndexView("downtrend", death_cross=True, below_trend=True, rsi2=40, above_sma5=False)
PLUNGE = IndexView("downtrend", death_cross=True, below_trend=True, rsi2=4, above_sma5=False)
BOUNCE = IndexView("downtrend", death_cross=True, below_trend=True, rsi2=80, above_sma5=True)


def swing(on=True, insurance=False):
    return clean_settings(True, 20_000.0, ("QQQ",), insurance, True, swing=on)


def apply(ledger, orders, price):
    """Fill every order at `price`, as the desk books it."""
    for order in orders:
        if order.side is Side.BUY:
            ledger.bought(order.symbol, int(order.dollars // price), price, TODAY.isoformat())
        else:
            ledger.sold(order.symbol, round(ledger.qty(order.symbol) * order.fraction), price,
                        park=order.park, insure=not order.swing)
        if order.swing:
            ledger.swung(order.symbol, order.swing)


def day(ledger, view, price, settings=None):
    settings = settings or swing()
    refresh(settings, ledger, {"QQQ": view})
    orders = plan(settings, ledger, {"QQQ": view}, TODAY, can_buy=True)
    apply(ledger, orders, price)
    return orders


def test_a_full_swing_out_on_a_dip_and_back_home():
    ledger = Ledger()
    ledger.bought("QQQ", 40, 500.0, "2026-10-01")
    ledger.contributed["QQQ"] = 20_000.0

    exit_ = day(ledger, BELOW, 480.0)
    assert [o.swing for o in exit_] == ["exit"] and ledger.qty("QQQ") == 0
    assert ledger.parked["QQQ"] == approx(19_200.0) and ledger.insured == []

    assert day(ledger, BELOW, 470.0) == []                       # waiting in cash for a sharp dip

    dip = day(ledger, PLUNGE, 440.0)
    assert [o.swing for o in dip] == ["dip"] and dip[0].dollars == approx(19_200.0)
    assert ledger.dipping == ["QQQ"] and ledger.qty("QQQ") == 43

    assert day(ledger, BELOW, 445.0) == []                       # holding the dip until the bounce

    sold = day(ledger, BOUNCE, 465.0)
    assert [o.swing for o in sold] == ["exit"] and ledger.qty("QQQ") == 0 and ledger.dipping == []

    back = day(ledger, ABOVE, 505.0)
    assert [o.swing for o in back] == ["back"] and ledger.swing_out == [] and ledger.qty("QQQ") > 0
    assert day(ledger, ABOVE, 510.0) == []                       # home: an ordinary holder again


def test_new_money_waits_below_the_trend_and_buys_the_dip():
    ledger = Ledger()

    assert day(ledger, BELOW, 480.0) == []
    first = day(ledger, PLUNGE, 450.0)
    assert first[0].swing == "dip" and first[0].dollars == approx(20_000.0)


def test_without_the_swing_or_off_the_nasdaq_the_robot_just_holds():
    ledger = Ledger()
    ledger.bought("QQQ", 40, 500.0, "2026-10-01")
    ledger.contributed["QQQ"] = 20_000.0
    assert day(ledger, BELOW, 480.0, swing(on=False)) == []

    spy = clean_settings(True, 20_000.0, ("SPY",), False, True, swing=True)
    held = Ledger()
    held.bought("SPY", 30, 600.0, "2026-10-01")
    held.contributed["SPY"] = 18_000.0
    orders = plan(spy, held, {"SPY": BELOW}, TODAY, can_buy=True)
    assert all(o.side is Side.BUY and not o.swing for o in orders)     # the S&P 500 is only held


def test_turning_the_swing_off_brings_the_parked_cash_home():
    ledger = Ledger()
    ledger.bought("QQQ", 40, 500.0, "2026-10-01")
    ledger.contributed["QQQ"] = 20_000.0
    day(ledger, BELOW, 480.0)

    orders = day(ledger, BELOW, 470.0, swing(on=False))

    assert ledger.swing_out == [] and orders[0].side is Side.BUY


def test_the_swing_state_survives_a_restart(tmp_path):
    ledger = Ledger()
    ledger.bought("QQQ", 10, 500.0, "2026-10-01")
    ledger.swung("QQQ", "dip")
    write_ledger(tmp_path, ledger)

    assert read_ledger(tmp_path).dipping == ["QQQ"]


def test_the_signals_match_the_research():
    rising = [100.0 + i for i in range(250)]
    assert swing_signals(rising) == (False, 100.0, True)

    crash = rising[:240] + [330.0, 320.0, 300.0, 280.0, 260.0, 240.0, 220.0, 200.0, 190.0, 180.0]
    below, rsi2, above5 = swing_signals(crash)
    assert below and rsi2 < 10 and not above5
    assert swing_signals(rising[:150]) == (False, 50.0, True)

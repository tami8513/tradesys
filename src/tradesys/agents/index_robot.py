"""The index robot: a smart holder of index funds.

Ten years of research (2017-2026) found no timing rule that beat simply holding
the index, so this robot does not try to. It holds the indices the client chose
as a long-term core, and is smart only about two things:

  * putting new money in: all at once on a dip or a breakout, otherwise in
    thirds, a few days apart, and it never waits more than a month;
  * optional bear-market insurance: in a death cross (the 50-day average below
    the 200-day, and the price below both) it holds half. That cost about one
    point a year in the last decade's quick recoveries, and is there for a long
    bear market like 2008.

Everything is measured at cost: the robot never sells because a price rose.
It sells only to follow the insurance, a lower budget, or an index the client
removed. This module decides; the engine executes.

The one exception is the optional Nasdaq swing. On QQQ, and only there, one
rule beat holding in every window of a 2016-2026 replay (10, 5 and 2 years):
hold while the price is above its 200-day average; below it, stay in cash and
buy only sharp dips (2-day RSI under 10), selling each dip once the price is
back above its 5-day average. The thresholds are the classic published ones,
not fitted to this data. The same rule did worse than holding on the Dow and
was mixed on the S&P 500, so it is offered for the Nasdaq alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tradesys.clients.index_sleeve import Ledger, SleeveSettings
from tradesys.core.enums import Side

MIN_ORDER = 100.0
TRANCHES = 3
TRANCHE_GAP_DAYS = 5
MAX_WAIT_DAYS = 20
TOLERANCE = 0.03
SWING_INDICES = frozenset({"QQQ"})
DIP_RSI = 10.0
INDEX_NAMES = {
    "SPY": "S&P 500", "QQQ": "נאסד\"ק 100", "DIA": "דאו ג'ונס",
    "IWM": "ראסל 2000", "MDY": "חברות בינוניות",
}


@dataclass(frozen=True, slots=True)
class IndexOrder:
    symbol: str
    side: Side
    reason: str
    dollars: float = 0.0    # buy: dollars to spend
    fraction: float = 0.0   # sell: share of the held quantity
    park: bool = False      # sell: keep the cash to buy back (insurance)
    swing: str = ""         # the Nasdaq swing's step: "exit", "dip" or "back"


@dataclass(frozen=True, slots=True)
class IndexView:
    signal: str             # the chart analyst's reading of the daily chart
    death_cross: bool
    below_trend: bool = False   # the close under its 200-day average
    rsi2: float = 50.0          # the 2-day RSI
    above_sma5: bool = True     # the close over its 5-day average


def swing_signals(closes: list[float]) -> tuple[bool, float, bool]:
    """(below the 200-day average, 2-day RSI, above the 5-day average), as the research read them."""
    if len(closes) < 201:
        return False, 50.0, True
    gain = loss = 0.0
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        up, down = max(change, 0.0), max(-change, 0.0)
        if i <= 2:
            gain, loss = gain + up / 2, loss + down / 2
        else:
            gain, loss = (gain + up) / 2, (loss + down) / 2
    rsi2 = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    last = closes[-1]
    return last < sum(closes[-200:]) / 200, rsi2, last > sum(closes[-5:]) / 5


def _days_since(ledger: Ledger, symbol: str, today: date) -> int:
    last = ledger.last_buy.get(symbol)
    return (today - date.fromisoformat(last)).days if last else 10_000


def plan(
    settings: SleeveSettings,
    ledger: Ledger,
    views: dict[str, IndexView],
    today: date,
    *,
    can_buy: bool,
    blocked: frozenset[str] = frozenset(),
) -> list[IndexOrder]:
    """Today's orders. `blocked` are indices the main robot trades right now.

    Turning the robot off freezes it: it keeps what it holds and does nothing.
    """
    if not settings.enabled:
        return []
    orders: list[IndexOrder] = []
    share = settings.budget / len(settings.indices)
    for symbol in sorted(set(settings.indices) | set(ledger.holdings)):
        if symbol in blocked or symbol not in views:
            continue
        name = INDEX_NAMES.get(symbol, symbol)
        swinging = settings.swing and symbol in SWING_INDICES
        insured = settings.bear_insurance and views[symbol].death_cross and not swinging
        contributed = ledger.contributed.get(symbol, 0.0)

        if symbol not in settings.indices:
            if ledger.qty(symbol):
                orders.append(IndexOrder(symbol, Side.SELL, f"{name} הוסר מהבחירה שלך, מוכרים את כולו",
                                         fraction=1.0))
            continue
        if swinging:
            order = _swing(name, symbol, views[symbol], ledger, max(0.0, share - contributed), can_buy)
            if order is not None:
                orders.append(order)
            if views[symbol].below_trend or symbol in ledger.swing_out or symbol in ledger.dipping:
                continue
        if insured and symbol not in ledger.insured and ledger.qty(symbol):
            orders.append(IndexOrder(
                symbol, Side.SELL,
                f"ביטוח שוק דובי: {name} במגמת ירידה ארוכה (ממוצע 50 מתחת לממוצע 200), "
                "מוכרים חצי ושומרים את הכסף לקנייה חוזרת",
                fraction=0.5, park=True,
            ))
            continue
        if contributed > share * (1 + TOLERANCE) and contributed - share >= MIN_ORDER:
            orders.append(IndexOrder(symbol, Side.SELL, f"התקציב ל-{name} ירד, מוכרים את ההפרש",
                                     fraction=(contributed - share) / contributed))
            continue
        if not can_buy or (insured and symbol in ledger.insured):
            continue

        new_money = max(0.0, (share / 2 if insured else share) - contributed)
        available = new_money + (0.0 if insured else ledger.parked.get(symbol, 0.0))
        waited = _days_since(ledger, symbol, today)
        tranche = share / TRANCHES
        signal = views[symbol].signal
        if signal in ("pullback", "breakout"):
            amount = available
            reason = (f"{name} בתיקון בתוך מגמת עלייה: מכניסים את כל ההפרש" if signal == "pullback"
                      else f"{name} פרץ לשיא חדש: מכניסים את כל ההפרש")
        elif signal == "stretched":
            amount = min(available, tranche) if waited >= MAX_WAIT_DAYS else 0.0
            reason = f"{name} עלה מהר, אבל לא מחכים לנצח: מכניסים שליש"
        else:
            amount = min(available, tranche) if waited >= TRANCHE_GAP_DAYS else 0.0
            reason = f"כניסה הדרגתית ל-{name}: עוד שליש"
        if amount >= MIN_ORDER:
            orders.append(IndexOrder(symbol, Side.BUY, reason, dollars=amount))
    return orders


def _swing(name: str, symbol: str, view: IndexView, ledger: Ledger, new_money: float,
           can_buy: bool) -> IndexOrder | None:
    """The Nasdaq swing's order for today, if any."""
    held, dipping = ledger.qty(symbol), symbol in ledger.dipping
    cash = ledger.parked.get(symbol, 0.0) + new_money
    if view.below_trend:
        if held and dipping and view.above_sma5:
            return IndexOrder(symbol, Side.SELL, f"{name}: הקפיצה אחרי הירידה הגיעה, מוכרים ומחכים לירידה "
                              "החדה הבאה", fraction=1.0, park=True, swing="exit")
        if held and not dipping:
            return IndexOrder(symbol, Side.SELL, f"{name} ירד מתחת לממוצע 200 יום: יוצאים למזומן ומחכים "
                              "לירידה חדה כדי לקנות", fraction=1.0, park=True, swing="exit")
        if not held and can_buy and view.rsi2 < DIP_RSI and cash >= MIN_ORDER:
            return IndexOrder(symbol, Side.BUY, f"{name} ירד בחדות (RSI של יומיים {view.rsi2:.0f}): קונים "
                              "את הירידה, ומוכרים כשהמחיר חוזר מעל ממוצע 5 ימים", dollars=cash, swing="dip")
        return None
    if (symbol in ledger.swing_out or dipping) and can_buy and cash >= MIN_ORDER:
        return IndexOrder(symbol, Side.BUY, f"{name} חזר מעל ממוצע 200 יום: חוזרים להחזיק",
                          dollars=cash, swing="back")
    return None


def refresh(settings: SleeveSettings, ledger: Ledger, views: dict[str, IndexView]) -> None:
    """Lift the insurance wherever the long downtrend is over, so the parked cash goes back in.

    The swing's marks are dropped once they mean nothing: the swing was turned off,
    or a dip held through the price climbing back above its 200-day average.
    """
    for symbol in list(ledger.insured):
        view = views.get(symbol)
        if view is not None and not (settings.bear_insurance and view.death_cross):
            ledger.recovered(symbol)
    for symbol in list(ledger.swing_out) + list(ledger.dipping):
        view = views.get(symbol)
        if not settings.swing or (view is not None and not view.below_trend and ledger.qty(symbol)
                                  and not ledger.parked.get(symbol)):
            ledger.swung(symbol, "back")

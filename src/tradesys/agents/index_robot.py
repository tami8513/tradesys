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


@dataclass(frozen=True, slots=True)
class IndexView:
    signal: str             # the chart analyst's reading of the daily chart
    death_cross: bool


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
        insured = settings.bear_insurance and views[symbol].death_cross
        contributed = ledger.contributed.get(symbol, 0.0)

        if symbol not in settings.indices:
            if ledger.qty(symbol):
                orders.append(IndexOrder(symbol, Side.SELL, f"{name} הוסר מהבחירה שלך, מוכרים את כולו",
                                         fraction=1.0))
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


def refresh(settings: SleeveSettings, ledger: Ledger, views: dict[str, IndexView]) -> None:
    """Lift the insurance wherever the long downtrend is over, so the parked cash goes back in."""
    for symbol in list(ledger.insured):
        view = views.get(symbol)
        if view is not None and not (settings.bear_insurance and view.death_cross):
            ledger.recovered(symbol)

"""Runs the index robot once a trading day and keeps its book apart from the main robot's.

It decides an hour before the close, on the day's nearly finished daily bar,
which is how the ten-year research decided. Orders go through the broker's
plain-order path, which keeps a partial fill: the ledger books exactly what
filled, and nothing sells the rest of the position behind the robot's back.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from tradesys.agents.chart_analyst import trend_view
from tradesys.agents.index_robot import IndexView, plan, refresh, swing_signals
from tradesys.clients.index_sleeve import book_equity, read_ledger, read_settings, write_ledger
from tradesys.core.enums import OrderType, Side, TimeInForce
from tradesys.core.errors import BrokerError
from tradesys.core.models import AccountSnapshot, OrderRequest

NEW_YORK = ZoneInfo("America/New_York")
RUN_AFTER = time(15, 0)
HISTORY_BARS = 260
CASH_HEADROOM = 0.98

AgentEvent = tuple[str, str, str, str]


def book_account(session, account: AccountSnapshot | None = None) -> AccountSnapshot:
    """The account as the main robot's risk checks must see it: without the index robot.

    Pass `account` when it was just read, to save the broker a second call.
    """
    account = account or session.broker.account()
    ledger = read_ledger(session.tenant.data_dir)
    if not (ledger.holdings or ledger.contributed or ledger.parked):
        return account
    prices = {p.symbol.upper(): p.market_price for p in session.broker.positions()}
    return replace(account, equity=book_equity(account.equity, ledger, prices))


def reserved_symbols(tenant_dir: Path) -> frozenset[str]:
    """Indices the main robot must not trade: one broker position cannot have two owners."""
    settings, ledger = read_settings(tenant_dir), read_ledger(tenant_dir)
    return frozenset(settings.indices if settings.enabled else ()) | frozenset(ledger.holdings)


def run_index_robot(session, *, can_buy: bool) -> list[AgentEvent]:
    data_dir = session.tenant.data_dir
    settings, ledger = read_settings(data_dir), read_ledger(data_dir)
    now = session.clock.now().astimezone(NEW_YORK)
    today = now.date().isoformat()
    if not settings.enabled or ledger.last_run == today or now.time() < RUN_AFTER:
        return []

    events: list[AgentEvent] = []
    held = {p.symbol.upper(): p.qty for p in session.broker.positions()}
    ledger.reconcile(held)
    views: dict[str, IndexView] = {}
    for symbol in sorted(set(settings.indices) | set(ledger.holdings)):
        try:
            bars = session.market_data.history(symbol, timeframe="1Day", limit=HISTORY_BARS)
            reading = trend_view(bars)
        except Exception as exc:  # one index's data outage skips it today, not the others
            events.append(("index", "failed", symbol, f"no data: {exc}"))
            continue
        if reading is not None:
            views[symbol] = IndexView(*reading, *swing_signals([b.close for b in bars]))
    if not views:
        return events    # no data at all: try again on the next cycle
    refresh(settings, ledger, views)

    blocked = frozenset(t.symbol.upper() for t in session.journal.open_trades())
    cash = session.broker.account().cash * CASH_HEADROOM
    for order in plan(settings, ledger, views, now.date(), can_buy=can_buy, blocked=blocked):
        price = session.market_data.last_price(order.symbol)
        if order.side is Side.BUY:
            qty = int(min(order.dollars, cash) // price)
        else:
            qty = min(ledger.qty(order.symbol), max(1, round(ledger.qty(order.symbol) * order.fraction)))
        if qty < 1:
            continue
        request = OrderRequest(
            client_id=session.client_id,
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            client_order_id=f"{session.client_id}-index-{order.symbol}-{today}-{order.side.value}",
            limit_price=price,
        )
        if session.journal.order_exists(request.client_order_id):
            continue
        try:
            result = session.broker.trade(request)
        except BrokerError as exc:
            events.append(("index", "failed", order.symbol, str(exc)))
            continue
        session.journal.record_order(request, result)
        if not result.filled_qty or result.filled_avg_price is None:
            events.append(("index", "unfilled", order.symbol, order.reason))
            continue
        filled, average = result.filled_qty, result.filled_avg_price
        if order.side is Side.BUY:
            ledger.bought(order.symbol, filled, average, today)
            cash -= filled * average
        else:
            ledger.sold(order.symbol, filled, average, park=order.park, insure=not order.swing)
        if order.swing:
            ledger.swung(order.symbol, order.swing)
        if order.side is Side.SELL:
            if not ledger.qty(order.symbol) and order.symbol not in settings.indices:
                ledger.release(order.symbol)
        events.append(("index", order.side.value, order.symbol, f"{filled}|{average:.2f}|{order.reason}"))

    ledger.last_run = today
    write_ledger(data_dir, ledger)
    return events

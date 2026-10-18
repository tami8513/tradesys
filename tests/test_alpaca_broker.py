"""Alpaca order handling, exercised offline against a scripted fake API.

Alpaca acknowledges a market order before it executes, and a bracket's legs hold
the shares. Without handling both, every Alpaca entry would go unjournaled and
every exit would be refused.
"""

from __future__ import annotations

from typing import Any

import pytest

from tradesys.brokers.alpaca import AlpacaBroker
from tradesys.clients.secrets import BrokerCredentials
from tradesys.core.enums import OrderStatus, OrderType, Side, TimeInForce
from tradesys.core.models import OrderRequest
from tradesys.core.tenancy import TenantContext

CREDS = BrokerCredentials("PKTEST", "secret", "https://paper-api.alpaca.markets", True)


def order_payload(status: str, filled_qty: int = 0, price: float | None = None) -> dict:
    return {
        "id": "ord-1",
        "client_order_id": "coid-1",
        "symbol": "SPY",
        "side": "buy",
        "qty": "3",
        "status": status,
        "filled_qty": str(filled_qty),
        "filled_avg_price": str(price) if price else None,
        "submitted_at": "2026-10-19T13:31:00Z",
    }


class FakeAlpaca:
    """Answers (method, path) with scripted payloads and records every call."""

    def __init__(self, script: dict[tuple[str, str], list[Any]]) -> None:
        self.script = script
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, path, payload=None, *, params=None, missing_ok=False):
        self.calls.append((method, path))
        replies = self.script.get((method, path), [None])
        return replies.pop(0) if len(replies) > 1 else replies[0]


def broker(tmp_path, fake: FakeAlpaca) -> AlpacaBroker:
    b = AlpacaBroker(
        TenantContext.create("tal_dahan", tmp_path), CREDS, fill_timeout=2.0, sleep=lambda _: None
    )
    b._request = fake
    return b


def entry() -> OrderRequest:
    return OrderRequest(
        client_id="tal_dahan",
        symbol="SPY",
        side=Side.BUY,
        qty=3,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        client_order_id="coid-1",
        stop_loss=560.0,
        take_profit=600.0,
    )


def test_entry_waits_for_the_fill_so_the_journal_gets_a_price(tmp_path):
    fake = FakeAlpaca({
        ("POST", "/v2/orders"): [order_payload("accepted")],
        ("GET", "/v2/orders/ord-1"): [order_payload("new"), order_payload("filled", 3, 580.25)],
    })
    result = broker(tmp_path, fake).submit(entry())

    assert result.status is OrderStatus.FILLED
    assert result.filled_avg_price == pytest.approx(580.25)
    assert ("DELETE", "/v2/orders/ord-1") not in fake.calls


def test_entry_that_never_fills_is_cancelled(tmp_path):
    fake = FakeAlpaca({
        ("POST", "/v2/orders"): [order_payload("accepted")],
        ("GET", "/v2/orders/ord-1"): [order_payload("new")] * 4 + [order_payload("canceled")],
    })
    result = broker(tmp_path, fake).submit(entry())

    assert ("DELETE", "/v2/orders/ord-1") in fake.calls
    assert result.status is OrderStatus.CANCELED
    assert ("DELETE", "/v2/positions/SPY") not in fake.calls


def test_partial_fill_on_a_cancelled_entry_is_sold_back(tmp_path):
    fake = FakeAlpaca({
        ("POST", "/v2/orders"): [order_payload("accepted")],
        ("GET", "/v2/orders/ord-1"): [order_payload("partially_filled", 1, 580.0)] * 4
        + [order_payload("canceled", 1, 580.0)],
    })
    result = broker(tmp_path, fake).submit(entry())

    assert result.status is OrderStatus.CANCELED
    assert ("DELETE", "/v2/positions/SPY") in fake.calls


def test_close_cancels_bracket_legs_before_selling(tmp_path):
    exit_order = {**order_payload("accepted"), "id": "ord-2", "side": "sell"}
    fake = FakeAlpaca({
        ("GET", "/v2/orders"): [[{"id": "leg-stop"}, {"id": "leg-target"}]],
        ("DELETE", "/v2/positions/SPY"): [exit_order],
        ("GET", "/v2/orders/ord-2"): [{**exit_order, "status": "filled", "filled_qty": "3",
                                       "filled_avg_price": "590.10"}],
    })
    result = broker(tmp_path, fake).close_position("spy")

    order_of_calls = [c for c in fake.calls if c[0] == "DELETE"]
    assert order_of_calls == [
        ("DELETE", "/v2/orders/leg-stop"),
        ("DELETE", "/v2/orders/leg-target"),
        ("DELETE", "/v2/positions/SPY"),
    ]
    assert result is not None and result.filled_avg_price == pytest.approx(590.10)


def test_close_with_no_position_reports_none(tmp_path):
    fake = FakeAlpaca({("GET", "/v2/orders"): [[]], ("DELETE", "/v2/positions/SPY"): [None]})
    assert broker(tmp_path, fake).close_position("SPY") is None

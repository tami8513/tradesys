from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from tradesys.clients.secrets import BrokerCredentials
from tradesys.core.enums import OrderStatus, OrderType, Side
from tradesys.core.errors import BrokerError
from tradesys.core.models import AccountSnapshot, OrderRequest, OrderResult, Position
from tradesys.core.tenancy import TenantContext

_STATUS_MAP = {
    "new": OrderStatus.ACCEPTED,
    "accepted": OrderStatus.ACCEPTED,
    "pending_new": OrderStatus.ACCEPTED,
    "filled": OrderStatus.FILLED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}
_FINAL_STATES = {"filled", "canceled", "expired", "rejected"}
MARKETABLE_LIMIT_PCT = 0.2


class AlpacaBroker:
    """Alpaca trading connection for exactly one tenant.

    Credentials arrive as an argument, resolved per client_id by the SecretResolver.
    Nothing here reads process-wide configuration, so two tenants running in the same
    process cannot end up sharing a connection or a key.
    """

    def __init__(
        self,
        tenant: TenantContext,
        credentials: BrokerCredentials,
        *,
        timeout: float = 10.0,
        # Long enough for a paper fill in a busy minute; on 2026-10-20/21 four
        # marketable entries went unfilled within 20s and were cancelled.
        fill_timeout: float = 45.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._tenant = tenant
        self.client_id = tenant.client_id
        self.paper = credentials.paper
        self._base_url = credentials.base_url.rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": credentials.key_id,
            "APCA-API-SECRET-KEY": credentials.secret_key,
        }
        self._timeout = timeout
        self._fill_timeout = fill_timeout
        self._sleep = sleep

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        params: Mapping[str, str] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("httpx is required for the Alpaca broker") from exc
        response = httpx.request(
            method,
            f"{self._base_url}{path}",
            headers=self._headers,
            json=dict(payload) if payload else None,
            params=dict(params) if params else None,
            timeout=self._timeout,
        )
        if missing_ok and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise BrokerError(f"alpaca {method} {path} -> {response.status_code}: {response.text}")
        if not response.content:
            return None
        return response.json()

    def account(self) -> AccountSnapshot:
        payload = self._request("GET", "/v2/account")
        return AccountSnapshot(
            client_id=self.client_id,
            equity=float(payload["equity"]),
            cash=float(payload["cash"]),
            buying_power=float(payload["buying_power"]),
            taken_at=datetime.now(timezone.utc),
        )

    def positions(self) -> list[Position]:
        payload = self._request("GET", "/v2/positions") or []
        return [
            Position(
                client_id=self.client_id,
                symbol=item["symbol"],
                qty=int(float(item["qty"])),
                avg_entry_price=float(item["avg_entry_price"]),
                market_price=float(item["current_price"]),
            )
            for item in payload
        ]

    def _order_body(self, order: OrderRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "symbol": order.symbol,
            "qty": str(order.qty),
            "side": order.side.value,
            "type": order.order_type.value,
            "time_in_force": order.time_in_force.value,
            "client_order_id": order.client_order_id,
        }
        if order.stop_loss is not None:
            # The legs inherit the entry's time in force, and a day stop expires at
            # the close: the first live position woke up with no stop at the broker.
            # An unfilled entry is cancelled within seconds anyway (see below).
            body["time_in_force"] = "gtc"
        if order.order_type is OrderType.LIMIT:
            if order.limit_price is None:
                raise BrokerError("limit order submitted without a limit price")
            # A few cents of room so a limit at the last print fills instead of
            # missing by a tick; it is bounded, so the size stays honest.
            room = MARKETABLE_LIMIT_PCT / 100
            room = 1 + room if order.side is Side.BUY else 1 - room
            body["limit_price"] = f"{order.limit_price * room:.2f}"
        if order.stop_loss is not None:
            body["order_class"] = "bracket" if order.take_profit else "oto"
            body["stop_loss"] = {"stop_price": f"{order.stop_loss:.2f}"}
            if order.take_profit is not None:
                body["take_profit"] = {"limit_price": f"{order.take_profit:.2f}"}
        return body

    def submit(self, order: OrderRequest) -> OrderResult:
        self._tenant.assert_owns(order)
        payload = self._request("POST", "/v2/orders", self._order_body(order))
        payload = self._await_final(payload)
        if payload.get("status") not in _FINAL_STATES:
            # The journal opens a trade only on a fill it saw. An entry still working
            # after the wait would fill later as a position nobody manages, so it is
            # pulled; if it filled in the meantime, the re-read reports the fill.
            self._request("DELETE", f"/v2/orders/{payload['id']}", missing_ok=True)
            payload = self._request("GET", f"/v2/orders/{payload['id']}")
        if payload.get("status") != "filled" and float(payload.get("filled_qty") or 0) > 0:
            # A partial fill on a pulled entry is not journaled either, so the shares
            # it bought are sold straight back rather than left unmanaged.
            self._request("DELETE", f"/v2/positions/{order.symbol.upper()}", missing_ok=True)
        return _parse_order(self.client_id, payload)

    def trade(self, order: OrderRequest) -> OrderResult:
        """A plain order that keeps whatever fills, for the index robot.

        Unlike an entry, a partial fill here is not an unmanaged position: the
        index robot books exactly the filled quantity. So the unfilled rest is
        cancelled and nothing is sold back. Selling the position, as `submit`
        does, would also sell the index robot's earlier shares.
        """
        self._tenant.assert_owns(order)
        if order.stop_loss is not None:
            raise BrokerError("trade() places plain orders; entries with a stop use submit()")
        payload = self._request("POST", "/v2/orders", self._order_body(order))
        payload = self._await_final(payload)
        if payload.get("status") not in _FINAL_STATES:
            self._request("DELETE", f"/v2/orders/{payload['id']}", missing_ok=True)
            payload = self._request("GET", f"/v2/orders/{payload['id']}")
        return _parse_order(self.client_id, payload)

    def resting_stop(self, symbol: str) -> float | None:
        """The stop price a working sell stop guards `symbol` at, if any."""
        for order in self._working_orders(symbol.upper()):
            if order.get("type") in ("stop", "stop_limit") and order.get("side") == "sell":
                return float(order.get("stop_price") or 0) or None
        return None

    def _working_orders(self, symbol: str) -> list[Mapping[str, Any]]:
        """Every order still working on `symbol`, bracket legs included.

        A bracket's stop leg sits in status "held" until the target leg is gone,
        and Alpaca's "open" listing leaves held orders out. Reading the parents
        with their legs nested is the only listing that shows the stop.
        """
        parents = self._request(
            "GET",
            "/v2/orders",
            params={"status": "all", "symbols": symbol, "nested": "true", "limit": "100"},
        ) or []
        working = []
        for parent in parents:
            for order in [parent, *(parent.get("legs") or [])]:
                if order.get("status") not in _FINAL_STATES:
                    working.append(order)
        return working

    def close_position(self, symbol: str) -> OrderResult | None:
        symbol = symbol.upper()
        # A bracket's stop and target legs hold the shares; Alpaca refuses to sell
        # held shares, so the legs are cancelled before the position is closed.
        for order in self._working_orders(symbol):
            if order.get("side") == "sell":
                self._request("DELETE", f"/v2/orders/{order['id']}", missing_ok=True)
        payload = self._request("DELETE", f"/v2/positions/{symbol}", missing_ok=True)
        if not payload:
            return None
        return _parse_order(self.client_id, self._await_final(payload))

    def protect(self, symbol: str, qty: int, stop_price: float) -> None:
        """Keep a resting stop at the broker, so the position is guarded even when
        this system is down: move it if it drifted, place a GTC one if none works."""
        symbol = symbol.upper()
        price = f"{stop_price:.2f}"
        stops = [
            o for o in self._working_orders(symbol)
            if o.get("type") in ("stop", "stop_limit") and o.get("side") == "sell"
        ]
        if not stops:
            self._request(
                "POST",
                "/v2/orders",
                {
                    "symbol": symbol,
                    "qty": str(qty),
                    "side": "sell",
                    "type": "stop",
                    "stop_price": price,
                    "time_in_force": "gtc",
                },
            )
            return
        for order in stops:
            if abs(float(order.get("stop_price") or 0) - stop_price) >= 0.01:
                self._request("PATCH", f"/v2/orders/{order['id']}", {"stop_price": price})

    def _await_final(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Re-read a just-submitted order until it fills or the wait runs out.

        Alpaca acknowledges a market order before it executes, so the submit
        response carries no fill price. The P&L is booked from the fill price.
        """
        waited = 0.0
        step = 0.5
        while payload.get("status") not in _FINAL_STATES and waited < self._fill_timeout:
            self._sleep(step)
            waited += step
            payload = self._request("GET", f"/v2/orders/{payload['id']}")
        return payload


def _parse_order(client_id: str, payload: Mapping[str, Any]) -> OrderResult:
    """Validate a broker order payload at the system boundary."""
    filled_price = payload.get("filled_avg_price")
    submitted = payload.get("submitted_at")
    return OrderResult(
        client_id=client_id,
        broker_order_id=str(payload["id"]),
        client_order_id=str(payload.get("client_order_id", "")),
        symbol=str(payload["symbol"]),
        side=Side(str(payload["side"])),
        qty=int(float(payload.get("qty", 0) or 0)),
        status=_STATUS_MAP.get(str(payload.get("status", "")), OrderStatus.ACCEPTED),
        filled_qty=int(float(payload.get("filled_qty", 0) or 0)),
        filled_avg_price=float(filled_price) if filled_price else None,
        submitted_at=(
            datetime.fromisoformat(str(submitted).replace("Z", "+00:00"))
            if submitted
            else None
        ),
        raw=dict(payload),
    )

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

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

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("httpx is required for the Alpaca broker") from exc
        response = httpx.request(
            method,
            f"{self._base_url}{path}",
            headers=self._headers,
            json=dict(payload) if payload else None,
            timeout=self._timeout,
        )
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

    def submit(self, order: OrderRequest) -> OrderResult:
        self._tenant.assert_owns(order)
        body: dict[str, Any] = {
            "symbol": order.symbol,
            "qty": str(order.qty),
            "side": order.side.value,
            "type": order.order_type.value,
            "time_in_force": order.time_in_force.value,
            "client_order_id": order.client_order_id,
        }
        if order.order_type is OrderType.LIMIT:
            if order.limit_price is None:
                raise BrokerError("limit order submitted without a limit price")
            body["limit_price"] = f"{order.limit_price:.2f}"
        if order.stop_loss is not None:
            body["order_class"] = "bracket" if order.take_profit else "oto"
            body["stop_loss"] = {"stop_price": f"{order.stop_loss:.2f}"}
            if order.take_profit is not None:
                body["take_profit"] = {"limit_price": f"{order.take_profit:.2f}"}

        payload = self._request("POST", "/v2/orders", body)
        return _parse_order(self.client_id, payload)

    def close_position(self, symbol: str) -> OrderResult | None:
        payload = self._request("DELETE", f"/v2/positions/{symbol.upper()}")
        return _parse_order(self.client_id, payload) if payload else None


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

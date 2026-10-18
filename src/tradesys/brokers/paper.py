from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tradesys.core.enums import OrderStatus, OrderType, Side, TimeInForce
from tradesys.core.errors import BrokerError
from tradesys.core.models import AccountSnapshot, OrderRequest, OrderResult, Position
from tradesys.core.fileio import locked, signature, write_json_atomic
from tradesys.core.tenancy import TenantContext
from tradesys.data.market_data import MarketDataProvider


class PaperBroker:
    """In-process paper broker with tenant-private, file-backed state.

    Fills are immediate at the last price. It models cash, positions and equity well
    enough to exercise the risk limits; it does not model slippage, partial fills,
    borrow availability or queue position.
    """

    def __init__(
        self,
        tenant: TenantContext,
        market_data: MarketDataProvider,
        *,
        starting_cash: float = 100_000.0,
    ) -> None:
        self._tenant = tenant
        self.client_id = tenant.client_id
        self.paper = True
        self._market_data = market_data
        self._path: Path = tenant.data_dir / "paper_account.json"
        self._starting_cash = starting_cash
        self._mtime = signature(self._path)
        self._state = self._load(starting_cash)

    def _refresh(self) -> None:
        """Re-read the ledger if another process (the scheduler) traded since."""
        current = signature(self._path)
        if current != self._mtime:
            self._state = self._load(self._starting_cash)
            self._mtime = current

    def _load(self, starting_cash: float) -> dict:
        if self._path.exists():
            state = json.loads(self._path.read_text(encoding="utf-8"))
            if state.get("client_id") != self.client_id:
                raise BrokerError(f"paper account file belongs to {state.get('client_id')!r}")
            return state
        return {"client_id": self.client_id, "cash": starting_cash, "positions": {}}

    def _flush(self) -> None:
        write_json_atomic(self._path, self._state)
        self._mtime = signature(self._path)

    def positions(self) -> list[Position]:
        self._refresh()
        result: list[Position] = []
        for symbol, holding in self._state["positions"].items():
            result.append(
                Position(
                    client_id=self.client_id,
                    symbol=symbol,
                    qty=int(holding["qty"]),
                    avg_entry_price=float(holding["avg_entry_price"]),
                    market_price=self._market_data.last_price(symbol),
                )
            )
        return result

    def account(self) -> AccountSnapshot:
        market_value = sum(p.market_value for p in self.positions())
        cash = float(self._state["cash"])
        return AccountSnapshot(
            client_id=self.client_id,
            equity=cash + market_value,
            cash=cash,
            buying_power=cash,
            taken_at=datetime.now(timezone.utc),
        )

    def submit(self, order: OrderRequest) -> OrderResult:
        self._tenant.assert_owns(order)
        if order.qty <= 0:
            raise BrokerError("order quantity must be positive")
        with locked(self._path):
            # Fill against the ledger as it is on disk now, not as this process
            # last saw it: a stale copy would overwrite the other process's fills.
            self._state = self._load(self._starting_cash)
            return self._fill(order)

    def _fill(self, order: OrderRequest) -> OrderResult:
        price = order.limit_price or self._market_data.last_price(order.symbol)
        notional = price * order.qty
        holdings = self._state["positions"]

        if order.side is Side.BUY:
            if notional > float(self._state["cash"]):
                raise BrokerError(
                    f"insufficient paper cash: need {notional:.2f}, "
                    f"have {self._state['cash']:.2f}"
                )
            existing = holdings.get(order.symbol)
            if existing:
                total_qty = existing["qty"] + order.qty
                existing["avg_entry_price"] = (
                    existing["avg_entry_price"] * existing["qty"] + notional
                ) / total_qty
                existing["qty"] = total_qty
            else:
                holdings[order.symbol] = {
                    "qty": order.qty,
                    "avg_entry_price": price,
                }
            self._state["cash"] = float(self._state["cash"]) - notional
        else:
            existing = holdings.get(order.symbol)
            if not existing or existing["qty"] < order.qty:
                raise BrokerError(f"cannot sell {order.qty} {order.symbol}: not held")
            existing["qty"] -= order.qty
            if existing["qty"] == 0:
                holdings.pop(order.symbol)
            self._state["cash"] = float(self._state["cash"]) + notional

        self._flush()
        return OrderResult(
            client_id=self.client_id,
            broker_order_id=f"paper-{uuid.uuid4().hex[:12]}",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            status=OrderStatus.FILLED,
            filled_qty=order.qty,
            filled_avg_price=price,
            submitted_at=datetime.now(timezone.utc),
        )

    def close_position(self, symbol: str) -> OrderResult | None:
        self._refresh()
        holding = self._state["positions"].get(symbol.upper())
        if not holding:
            return None
        return self.submit(
            OrderRequest(
                client_id=self.client_id,
                symbol=symbol.upper(),
                side=Side.SELL,
                qty=int(holding["qty"]),
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                client_order_id=f"close-{uuid.uuid4().hex[:12]}",
            )
        )

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Mapping

from tradesys.core.enums import (
    DirectiveAction,
    DirectiveScope,
    MemoryKind,
    OrderStatus,
    OrderType,
    SetupType,
    Side,
    TimeInForce,
)


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Candidate:
    """A trade idea produced by the Scout. Carries no authority to trade."""

    client_id: str
    symbol: str
    setup: SetupType
    side: Side
    entry: float
    stop: float
    target: float
    confidence: float
    rationale: str
    features: Mapping[str, float] = field(default_factory=dict)
    memory_context: tuple[str, ...] = ()

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_per_share(self) -> float:
        return abs(self.target - self.entry)

    @property
    def reward_risk(self) -> float:
        risk = self.risk_per_share
        return self.reward_per_share / risk if risk > 0 else 0.0


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """An executable instruction.

    Entry orders may only be constructed by the Risk Manager. Exits are not gated:
    closing risk is always permitted, including while the tenant is halted.
    """

    client_id: str
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    time_in_force: TimeInForce
    client_order_id: str
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    client_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: int
    status: OrderStatus
    filled_qty: int = 0
    filled_avg_price: float | None = None
    submitted_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Position:
    client_id: str
    symbol: str
    qty: int
    avg_entry_price: float
    market_price: float

    @property
    def market_value(self) -> float:
        return self.qty * self.market_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.market_price - self.avg_entry_price) * self.qty


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    client_id: str
    equity: float
    cash: float
    buying_power: float
    taken_at: datetime


@dataclass(frozen=True, slots=True)
class DayStats:
    """Per-tenant, per-session counters the Risk Manager enforces limits against."""

    client_id: str
    day: date
    realized_pnl: float = 0.0
    trades_opened: int = 0
    peak_equity: float = 0.0


@dataclass(frozen=True, slots=True)
class TradeRecord:
    trade_id: int
    client_id: str
    symbol: str
    setup: SetupType
    side: Side
    qty: int
    entry_price: float
    stop_price: float
    opened_at: datetime
    target_price: float | None = None
    exit_price: float | None = None
    closed_at: datetime | None = None
    realized_pnl: float | None = None
    rationale: str = ""
    initial_stop: float | None = None   # stop at entry; stop_price moves when trailing
    peak_price: float | None = None     # best price seen while open, for the trail

    @property
    def is_closed(self) -> bool:
        return self.closed_at is not None

    @property
    def risk_per_share(self) -> float:
        # Measured from the entry stop: a trailed stop at breakeven would otherwise
        # make every winner's R-multiple zero and corrupt what the system learns.
        stop = self.initial_stop if self.initial_stop is not None else self.stop_price
        return abs(self.entry_price - stop)

    def r_multiple(self) -> float:
        """Realized P&L expressed in units of the risk originally taken."""
        risk = self.risk_per_share * self.qty
        if risk <= 0 or self.realized_pnl is None:
            return 0.0
        return self.realized_pnl / risk

    def holding_period(self, until: datetime) -> timedelta:
        return until - self.opened_at


@dataclass(frozen=True, slots=True)
class FeedbackDirective:
    """Structured, machine-enforceable form of one operator instruction.

    Free text is kept for RAG context, but every *binding* effect on trading flows
    through these fields, so behaviour cannot drift with prompt phrasing.
    """

    client_id: str
    action: DirectiveAction
    scope: DirectiveScope
    symbols: tuple[str, ...] = ()
    setups: tuple[SetupType, ...] = ()
    weight: float = 1.0
    source_text: str = ""
    trade_id: int | None = None
    created_at: datetime | None = None
    directive_id: int | None = None

    def matches(self, candidate: Candidate) -> bool:
        if self.scope is DirectiveScope.GLOBAL:
            return True
        symbol_ok = not self.symbols or candidate.symbol.upper() in self.symbols
        setup_ok = not self.setups or candidate.setup in self.setups
        if self.scope is DirectiveScope.SYMBOL:
            return symbol_ok and setup_ok
        return setup_ok and symbol_ok


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    client_id: str
    kind: MemoryKind
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    record_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MemoryHit:
    record: MemoryRecord
    score: float

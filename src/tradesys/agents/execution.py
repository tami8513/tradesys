from __future__ import annotations

from dataclasses import dataclass

from tradesys.agents.base import Agent
from tradesys.agents.risk_manager import RiskDecision
from tradesys.brokers.base import Broker
from tradesys.clients.profile import ClientProfile
from tradesys.core.clock import Clock
from tradesys.core.enums import OrderStatus
from tradesys.core.errors import BrokerError, RiskViolation
from tradesys.core.models import OrderResult
from tradesys.core.tenancy import TenantContext
from tradesys.persistence.journal import Journal


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    client_id: str
    symbol: str
    submitted: bool
    reason: str = ""
    trade_id: int | None = None
    result: OrderResult | None = None


class ExecutionAgent(Agent):
    """Places orders. Accepts a RiskDecision and nothing else.

    Three guarantees this class is responsible for:

      * **No unapproved orders.** The only public entry point takes a RiskDecision;
        there is no code path from a Candidate straight to the broker.
      * **No accidental live trading.** Live mode requires both the tenant's profile
        and the process-wide TRADESYS_ALLOW_LIVE_TRADING switch.
      * **No double fills.** The Risk Manager mints a deterministic client_order_id
        per (tenant, symbol, setup, day); a replayed cycle is a no-op.
    """

    name = "execution"

    def __init__(
        self,
        tenant: TenantContext,
        profile: ClientProfile,
        broker: Broker,
        journal: Journal,
        *,
        allow_live_trading: bool = False,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(tenant, profile, clock)
        if broker.client_id != tenant.client_id:
            raise RiskViolation("broker connection belongs to a different tenant")
        if not broker.paper and not allow_live_trading:
            raise RiskViolation(
                "live broker connection refused: set TRADESYS_ALLOW_LIVE_TRADING=1 "
                "to enable real-money execution for this process"
            )
        self._broker = broker
        self._journal = journal

    def execute(self, decision: RiskDecision) -> ExecutionOutcome:
        self.tenant.assert_owns(decision)
        if not decision.approved or decision.order is None:
            raise RiskViolation(
                f"execution called with an unapproved decision for "
                f"{decision.candidate.symbol}"
            )

        order = decision.order
        self.tenant.assert_owns(order)

        if self._journal.order_exists(order.client_order_id):
            return ExecutionOutcome(
                client_id=self.client_id,
                symbol=order.symbol,
                submitted=False,
                reason="duplicate client_order_id; this idea was already sent today",
            )

        try:
            result = self._broker.submit(order)
        except BrokerError as exc:
            self.log.warning("broker rejected %s: %s", order.symbol, exc)
            return ExecutionOutcome(
                client_id=self.client_id,
                symbol=order.symbol,
                submitted=False,
                reason=f"broker rejected: {exc}",
            )

        self._journal.record_order(order, result)

        trade_id: int | None = None
        if result.status is OrderStatus.FILLED and result.filled_avg_price is not None:
            trade_id = self._journal.open_trade(
                decision.candidate,
                order,
                result.filled_avg_price,
                self.clock.now(),
            )
            self._journal.record_trade_opened()
            self.log.info(
                "filled %s x%d @ %.2f (trade #%d)",
                order.symbol,
                result.filled_qty,
                result.filled_avg_price,
                trade_id,
            )

        return ExecutionOutcome(
            client_id=self.client_id,
            symbol=order.symbol,
            submitted=True,
            trade_id=trade_id,
            result=result,
        )

    def close(self, symbol: str, reason: str) -> ExecutionOutcome:
        """Exits are never gated by risk limits — reducing exposure is always allowed."""
        result = self._broker.close_position(symbol)
        if result is None:
            return ExecutionOutcome(
                client_id=self.client_id,
                symbol=symbol.upper(),
                submitted=False,
                reason="no open position",
            )
        self.log.info("closed %s: %s", symbol.upper(), reason)
        return ExecutionOutcome(
            client_id=self.client_id,
            symbol=symbol.upper(),
            submitted=True,
            reason=reason,
            result=result,
        )

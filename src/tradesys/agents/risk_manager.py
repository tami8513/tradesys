from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Sequence

from tradesys.agents.base import Agent
from tradesys.clients.profile import ClientProfile, WatchlistEntry
from tradesys.core.clock import Clock, trading_day
from tradesys.core.enums import OrderType, Side, TimeInForce
from tradesys.core.models import (
    AccountSnapshot,
    Candidate,
    DayStats,
    OrderRequest,
    Position,
)
from tradesys.core.tenancy import TenantContext
from tradesys.persistence.journal import Journal

_ORDER_NAMESPACE = uuid.UUID("6f4d1e6a-2a1e-4f6d-9c2b-0f5a8c9d3e11")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The only object that authorises an entry.

    The Execution agent accepts nothing else, and a decision with ``approved=False``
    never carries an order. Every rejection lists its reasons so the audit trail
    explains itself.
    """

    client_id: str
    candidate: Candidate
    approved: bool
    reasons: tuple[str, ...] = ()
    qty: int = 0
    risk_amount: float = 0.0
    order: OrderRequest | None = None


@dataclass(frozen=True, slots=True)
class PreflightResult:
    ok: bool
    reason: str = ""


class RiskManager(Agent):
    """Deterministic, rule-based gate that sits between the AI and the broker.

    Design rule: this class contains no model calls, no randomness and no
    configuration it did not receive from the tenant's own RiskProfile. It is the
    system's last line of defence, so it must be readable end-to-end and testable
    without any infrastructure.

    Rejections are silent by design: a blocked idea is simply not traded.
    """

    name = "risk"

    def __init__(
        self,
        tenant: TenantContext,
        profile: ClientProfile,
        journal: Journal,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(tenant, profile, clock)
        self._journal = journal

    # -------------------------------------------------------------- portfolio

    def preflight(self, account: AccountSnapshot) -> PreflightResult:
        """Portfolio-level checks, run once per cycle before any idea is considered.

        A breach latches: the tenant stays halted until an operator clears it, so a
        recovering equity curve cannot silently resume trading after a blow-up.
        """
        self.tenant.assert_owns(account)
        limits = self.profile.risk

        halted, reason = self._journal.halt_state()
        if halted:
            return PreflightResult(False, f"tenant halted: {reason}")

        stats = self._journal.roll_session(trading_day(self.clock), account.equity)

        if stats.peak_equity > 0:
            drawdown_pct = (stats.peak_equity - account.equity) / stats.peak_equity * 100
            if drawdown_pct >= limits.max_drawdown_pct:
                reason = (
                    f"drawdown {drawdown_pct:.2f}% >= limit {limits.max_drawdown_pct:.2f}% "
                    f"(peak {stats.peak_equity:.2f}, equity {account.equity:.2f})"
                )
                self._journal.halt(reason)
                self.log.error("HALT %s", reason)
                return PreflightResult(False, reason)

        day_start = self._journal.day_start_equity()
        if day_start > 0 and stats.realized_pnl < 0:
            loss_pct = abs(stats.realized_pnl) / day_start * 100
            if loss_pct >= limits.max_daily_loss_pct:
                reason = (
                    f"daily loss {loss_pct:.2f}% >= limit {limits.max_daily_loss_pct:.2f}%"
                )
                self._journal.halt(reason)
                self.log.error("HALT %s", reason)
                return PreflightResult(False, reason)

        return PreflightResult(True)

    # ------------------------------------------------------------- per-trade

    def evaluate(
        self,
        candidate: Candidate,
        account: AccountSnapshot,
        positions: Sequence[Position],
        stats: DayStats,
    ) -> RiskDecision:
        self.tenant.assert_owns(candidate)
        self.tenant.assert_owns(account)
        limits = self.profile.risk
        reasons: list[str] = []

        entry = self.profile.entry_for(candidate.symbol)
        if entry is None:
            reasons.append(f"{candidate.symbol} is not on the client watchlist")
        elif not self.profile.allows_setup(candidate.symbol, candidate.setup):
            reasons.append(
                f"setup {candidate.setup.value} not enabled for {candidate.symbol}"
            )

        if candidate.side is Side.SELL and not limits.allow_short:
            reasons.append("short selling is disabled for this client")

        if candidate.entry < limits.min_price:
            reasons.append(
                f"price {candidate.entry:.2f} below min_price {limits.min_price:.2f}"
            )

        if candidate.risk_per_share <= 0:
            reasons.append("stop is missing or equal to entry")
        elif candidate.side is Side.BUY and candidate.stop >= candidate.entry:
            reasons.append("long stop must sit below entry")
        elif candidate.side is Side.SELL and candidate.stop <= candidate.entry:
            reasons.append("short stop must sit above entry")

        if candidate.reward_risk < limits.min_reward_risk:
            reasons.append(
                f"reward:risk {candidate.reward_risk:.2f} below minimum "
                f"{limits.min_reward_risk:.2f}"
            )

        if len(positions) >= limits.max_open_positions:
            reasons.append(
                f"{len(positions)} open positions at limit {limits.max_open_positions}"
            )

        if any(p.symbol == candidate.symbol for p in positions):
            reasons.append(f"already holding {candidate.symbol}; pyramiding is disabled")

        if stats.trades_opened >= limits.max_trades_per_day:
            reasons.append(
                f"{stats.trades_opened} trades today at limit {limits.max_trades_per_day}"
            )

        if reasons:
            return self._reject(candidate, reasons)

        qty, risk_amount, sizing_reasons = self._size(candidate, account, entry)
        if qty < 1:
            return self._reject(candidate, sizing_reasons or ["position sizes to zero shares"])

        order = OrderRequest(
            client_id=self.client_id,
            symbol=candidate.symbol,
            side=candidate.side,
            qty=qty,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            client_order_id=self._client_order_id(candidate),
            limit_price=round(candidate.entry, 2),
            stop_loss=round(candidate.stop, 2),
            take_profit=round(candidate.target, 2),
        )
        self.log.info(
            "approved %s %s x%d (risk %.2f, R:R %.2f)",
            candidate.side.value,
            candidate.symbol,
            qty,
            risk_amount,
            candidate.reward_risk,
        )
        return RiskDecision(
            client_id=self.client_id,
            candidate=candidate,
            approved=True,
            reasons=tuple(sizing_reasons),
            qty=qty,
            risk_amount=risk_amount,
            order=order,
        )

    # ---------------------------------------------------------------- sizing

    def _size(
        self,
        candidate: Candidate,
        account: AccountSnapshot,
        entry: WatchlistEntry | None,
    ) -> tuple[int, float, list[str]]:
        """Volatility-based sizing, then every cap applied as a hard minimum."""
        limits = self.profile.risk
        notes: list[str] = []

        risk_budget = account.equity * (limits.risk_per_trade_pct / 100.0)
        qty = int(risk_budget // candidate.risk_per_share)
        if qty < 1:
            return 0, 0.0, [
                f"risk budget {risk_budget:.2f} cannot cover one share at "
                f"{candidate.risk_per_share:.2f} of stop distance"
            ]

        position_cap_pct = limits.max_position_pct
        if entry is not None and entry.max_position_pct is not None:
            position_cap_pct = min(position_cap_pct, entry.max_position_pct)
        max_notional = account.equity * (position_cap_pct / 100.0)
        capped = int(max_notional // candidate.entry)
        if capped < qty:
            notes.append(f"reduced to {capped} shares by {position_cap_pct:.2f}% position cap")
            qty = capped

        if not limits.allow_leverage:
            affordable = int(account.buying_power // candidate.entry)
            if affordable < qty:
                notes.append(f"reduced to {affordable} shares by available buying power")
                qty = affordable

        avg_volume = candidate.features.get("avg_volume", 0.0)
        if avg_volume > 0:
            liquidity_cap = int(avg_volume * limits.max_position_pct_of_adv)
            if liquidity_cap < qty:
                notes.append(f"reduced to {liquidity_cap} shares by liquidity cap")
                qty = liquidity_cap

        qty = max(qty, 0)
        return qty, qty * candidate.risk_per_share, notes

    def _client_order_id(self, candidate: Candidate) -> str:
        """Deterministic per (tenant, symbol, setup, day) so retries cannot double-fill."""
        seed = (
            f"{self.client_id}|{candidate.symbol}|{candidate.setup.value}|"
            f"{trading_day(self.clock).isoformat()}"
        )
        return uuid.uuid5(_ORDER_NAMESPACE, seed).hex

    def _reject(self, candidate: Candidate, reasons: list[str]) -> RiskDecision:
        self.log.info("rejected %s: %s", candidate.symbol, "; ".join(reasons))
        return RiskDecision(
            client_id=self.client_id,
            candidate=candidate,
            approved=False,
            reasons=tuple(reasons),
        )

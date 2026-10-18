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
from tradesys.data.earnings import EarningsCalendar, NullEarningsCalendar
from tradesys.data.regime import MarketRegime
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


@dataclass(frozen=True, slots=True)
class FloorState:
    """Where the book stands against its capital floor.

    `available` is the cushion left after every open stop is assumed hit. New
    entries only ever draw on that, which is what keeps the book above the floor
    even in a worst case where all stops fire together.
    """

    base: float
    floor: float
    equity: float
    open_risk: float
    per_trade_pct: float

    @property
    def cushion(self) -> float:
        return self.equity - self.floor

    @property
    def available(self) -> float:
        return self.cushion - self.open_risk

    @property
    def per_trade_budget(self) -> float:
        return max(0.0, self.available) * self.per_trade_pct / 100.0

    @property
    def worst_case_equity(self) -> float:
        """Equity if every open stop were hit right now."""
        return self.equity - self.open_risk


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
        earnings: EarningsCalendar | None = None,
    ) -> None:
        super().__init__(tenant, profile, clock)
        self._journal = journal
        # Defaults to "unknown for everything", which is inert while the earnings
        # filter is off and blocking once it is switched on without a data source.
        self._earnings = earnings or NullEarningsCalendar()

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

    # ----------------------------------------------------------- capital floor

    def floor_state(self, account: AccountSnapshot, stats: DayStats) -> FloorState | None:
        """The book against its floor, or None when no floor is configured."""
        limits = self.profile.risk
        if limits.capital_floor_pct is None:
            return None
        if limits.floor_ratchet:
            base = max(stats.peak_equity, account.equity, limits.capital_floor_base or 0.0)
        elif limits.capital_floor_base is not None:
            base = limits.capital_floor_base
        else:
            base = self._journal.capital_base() or account.equity
        return FloorState(
            base=base,
            floor=base * limits.capital_floor_pct / 100.0,
            equity=account.equity,
            open_risk=self._journal.open_risk(),
            per_trade_pct=limits.floor_cushion_per_trade_pct,
        )

    # ------------------------------------------------------------- per-trade

    def evaluate(
        self,
        candidate: Candidate,
        account: AccountSnapshot,
        positions: Sequence[Position],
        stats: DayStats,
        regime: MarketRegime | None = None,
        *,
        conviction: float = 1.0,
    ) -> RiskDecision:
        """Gate one candidate.

        `regime` is passed in rather than fetched so this class stays free of I/O
        and non-determinism. None means the regime could not be read, which is
        treated as blocking for regime-gated setups — see _regime_reason.

        `conviction` is the share of the per-trade risk ceiling to use. It is
        clamped to (0, 1] here, at the trust boundary: no caller can size a trade
        above the ceiling the client's risk level set.
        """
        self.tenant.assert_owns(candidate)
        self.tenant.assert_owns(account)
        limits = self.profile.risk
        reasons: list[str] = []

        earnings_reason = self._earnings_reason(candidate)
        if earnings_reason:
            reasons.append(earnings_reason)

        regime_reason = self._regime_reason(candidate, regime)
        if regime_reason:
            reasons.append(regime_reason)

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

        floor = self.floor_state(account, stats)
        if floor is not None and floor.per_trade_budget <= 0:
            reasons.append(
                f"at capital floor: equity {floor.equity:.2f}, floor {floor.floor:.2f}, "
                f"{floor.open_risk:.2f} already at risk in open positions — no new "
                f"entries until the cushion recovers"
            )

        if reasons:
            return self._reject(candidate, reasons)

        qty, risk_amount, sizing_reasons = self._size(
            candidate, account, entry, floor, conviction
        )
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
            # A resting take-profit leg would sell at the target no matter what the
            # monitor decides, so letting winners run means sending the stop alone.
            # The target still lives in the journal, from the candidate.
            take_profit=None if limits.let_winners_run else round(candidate.target, 2),
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

    # ------------------------------------------------------- external filters

    def _earnings_reason(self, candidate: Candidate) -> str | None:
        """Block entries too close to a scheduled report.

        Unknown earnings dates block rather than pass. The reason this filter was
        switched on is that a gap can jump the stop; not knowing whether a report
        is due tomorrow is exactly the state it exists to prevent. The rejection
        says so explicitly, so a missing calendar is loud rather than silent.
        """
        window = self.profile.risk.avoid_earnings_within_days
        if window is None:
            return None

        today = trading_day(self.clock)
        found = self._earnings.lookup(candidate.symbol, today)

        if not found.covered:
            return (
                f"no earnings coverage for {candidate.symbol} and the earnings "
                f"filter is on ({window}d) — add it to the calendar to trade this "
                f"name (an empty list means 'never reports', e.g. an ETF)"
            )
        if found.next_date is None:
            # Covered with nothing scheduled: an ETF, an index, or a name whose
            # next report is beyond the horizon. Nothing to avoid.
            return None

        days = (found.next_date - today).days
        next_report = found.next_date
        if days <= window:
            return (
                f"{candidate.symbol} reports in {days}d ({next_report.isoformat()}), "
                f"inside the {window}d earnings window — an ATR stop cannot hold "
                f"through a gap"
            )
        return None

    def _regime_reason(
        self, candidate: Candidate, regime: MarketRegime | None
    ) -> str | None:
        """Block regime-gated setups while the broad market is risk-off."""
        blocked = self.profile.risk.regime_blocked_setups
        if not blocked or candidate.setup not in blocked:
            return None

        limits = self.profile.risk
        if regime is None:
            return (
                f"market regime unavailable ({limits.regime_symbol}) and "
                f"{candidate.setup.value} is regime-gated"
            )
        if not regime.risk_on:
            return (
                f"{candidate.setup.value} blocked in a risk-off market: "
                f"{regime.describe()}"
            )
        return None

    # ---------------------------------------------------------------- sizing

    def _size(
        self,
        candidate: Candidate,
        account: AccountSnapshot,
        entry: WatchlistEntry | None,
        floor: FloorState | None = None,
        conviction: float = 1.0,
    ) -> tuple[int, float, list[str]]:
        """Volatility-based sizing, then every cap applied as a hard minimum."""
        limits = self.profile.risk
        notes: list[str] = []

        share = min(1.0, max(0.0, conviction))
        risk_budget = account.equity * (limits.risk_per_trade_pct / 100.0) * share
        if share < 1.0:
            notes.append(f"risk budget at {share:.0%} of the ceiling by conviction")
        if floor is not None and floor.per_trade_budget < risk_budget:
            notes.append(
                f"risk budget reduced to {floor.per_trade_budget:.2f} by the capital "
                f"floor ({floor.available:.2f} of cushion left above {floor.floor:.2f})"
            )
            risk_budget = floor.per_trade_budget
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

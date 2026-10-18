from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tradesys.agents.base import Agent
from tradesys.agents.execution import ExecutionAgent
from tradesys.agents.reflection import ReflectionAgent
from tradesys.brokers.base import Broker
from tradesys.clients.profile import ClientProfile
from tradesys.core.clock import Clock
from tradesys.core.enums import ExitReason, Side
from tradesys.core.errors import TradesysError
from tradesys.core.models import MemoryRecord, TradeRecord
from tradesys.core.tenancy import TenantContext
from tradesys.data.indicators import closes, sma
from tradesys.data.market_data import MarketDataProvider
from tradesys.persistence.journal import Journal

TREND_SMA_PERIOD = 10
TREND_SLOPE_BARS = 3


@dataclass(frozen=True, slots=True)
class ExitSignal:
    trade_id: int
    symbol: str
    reason: ExitReason
    price: float
    detail: str


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    trade: TradeRecord
    reason: ExitReason
    exit_price: float
    lessons: tuple[MemoryRecord, ...]


@dataclass(slots=True)
class MonitorReport:
    client_id: str
    checked_at: datetime
    open_trades: int = 0
    closed: list[ClosedTrade] = field(default_factory=list)
    adjusted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def realized_pnl(self) -> float:
        return sum(c.trade.realized_pnl or 0.0 for c in self.closed)

    def summary(self) -> str:
        if not self.closed and not self.errors and not self.adjusted:
            return f"[{self.client_id}] {self.open_trades} position(s) held, no exits"
        parts = [f"[{self.client_id}] {self.open_trades} checked"]
        parts.extend(self.adjusted)
        for closed in self.closed:
            parts.append(
                f"{closed.trade.symbol} closed on {closed.reason.value} "
                f"@ {closed.exit_price:.2f} ({closed.trade.realized_pnl or 0.0:+.2f})"
            )
        parts.extend(f"error: {e}" for e in self.errors)
        return " | ".join(parts)


class PositionMonitor(Agent):
    """Watches open positions and closes them when their exit conditions are met.

    This is the other half of the Risk Manager. The Risk Manager decides what risk may
    be *taken*; the monitor is what actually *removes* it. Without it, a stop price is
    a number in a database rather than a real exit.

    Four exit triggers, checked in a deliberate order of precedence:

      1. **Broker reconciliation** — the position is gone from the broker (a bracket
         order filled server-side, or someone closed it by hand). The journal is
         corrected rather than left drifting from reality.
      2. **Stop loss** — checked before the target, so a bar that gapped through both
         is booked as the loss. Assuming the good fill would flatter every backtest and
         every P&L report built on this journal.
      3. **Take profit**.
      4. **Time stop** — optional; closes theses that never resolved.

    Exits are never gated by the Risk Manager or by a halt: reducing exposure is always
    permitted, especially when the tenant is halted.
    """

    name = "monitor"

    def __init__(
        self,
        tenant: TenantContext,
        profile: ClientProfile,
        market_data: MarketDataProvider,
        broker: Broker,
        journal: Journal,
        execution: ExecutionAgent,
        reflection: ReflectionAgent,
        clock: Clock | None = None,
        *,
        max_holding_days: int | None = None,
        reconcile_grace_seconds: float = 60.0,
    ) -> None:
        super().__init__(tenant, profile, clock)
        for component in (broker, execution, reflection, journal):
            if getattr(component, "client_id", None) != tenant.client_id:
                raise ValueError(
                    f"{type(component).__name__} belongs to a different tenant"
                )
        self._market_data = market_data
        self._broker = broker
        self._journal = journal
        self._execution = execution
        self._reflection = reflection
        self._max_holding_days = max_holding_days
        self._reconcile_grace = timedelta(seconds=reconcile_grace_seconds)

    # ------------------------------------------------------------- one sweep

    def check_once(self) -> MonitorReport:
        """Poll every open position once and act on whatever has triggered."""
        now = self.clock.now()
        report = MonitorReport(client_id=self.client_id, checked_at=now)

        open_trades = self._journal.open_trades()
        report.open_trades = len(open_trades)
        if not open_trades:
            return report

        try:
            held = {position.symbol.upper() for position in self._broker.positions()}
        except Exception as exc:
            # Deliberately broad: a supervisory loop that dies on a transient broker or
            # data error stops managing risk entirely, which is worse than any error it
            # could raise. Without a position snapshot, reconciliation would also read
            # every open trade as orphaned and close the whole book.
            report.errors.append(f"position snapshot failed, skipping sweep: {exc}")
            return report

        by_symbol: dict[str, list[TradeRecord]] = {}
        for trade in open_trades:
            by_symbol.setdefault(trade.symbol.upper(), []).append(trade)

        for symbol, trades in by_symbol.items():
            try:
                price = self._market_data.last_price(symbol)
            except Exception as exc:  # a data outage must not kill the loop
                report.errors.append(f"{symbol}: price unavailable ({exc})")
                continue

            trade = trades[0]
            if (
                self.profile.risk.trailing_stop
                and trade.side is Side.BUY
                and symbol in held
            ):
                trade = self._trail(symbol, trade, price, report)

            signal = self._evaluate(trade, price, symbol in held, now)
            if signal is None:
                continue
            try:
                self._close(symbol, trades, signal, report)
            except TradesysError as exc:
                report.errors.append(f"{symbol}: exit failed ({exc})")

        if report.closed:
            self.log.info(report.summary())
        return report

    def _evaluate(
        self,
        trade: TradeRecord,
        price: float,
        still_held: bool,
        now: datetime,
    ) -> ExitSignal | None:
        opened_recently = now - trade.opened_at < self._reconcile_grace
        if not still_held and not opened_recently:
            return ExitSignal(
                trade.trade_id,
                trade.symbol,
                ExitReason.BROKER_CLOSED,
                price,
                "position is no longer open at the broker",
            )

        long = trade.side is Side.BUY
        stop_hit = price <= trade.stop_price if long else price >= trade.stop_price
        if stop_hit:
            return ExitSignal(
                trade.trade_id,
                trade.symbol,
                ExitReason.STOP_LOSS,
                price,
                f"{price:.2f} through stop {trade.stop_price:.2f}",
            )

        if trade.target_price is not None:
            target_hit = (
                price >= trade.target_price if long else price <= trade.target_price
            )
            if target_hit:
                return ExitSignal(
                    trade.trade_id,
                    trade.symbol,
                    ExitReason.TAKE_PROFIT,
                    price,
                    f"{price:.2f} reached target {trade.target_price:.2f}",
                )

        # The time stop closes theses that never resolved. A long whose stop has
        # been trailed to breakeven or better has resolved, and is left to run.
        profit_locked = long and trade.stop_price >= trade.entry_price
        if self._max_holding_days is not None and not profit_locked:
            held_days = (now - trade.opened_at).total_seconds() / 86400
            if held_days >= self._max_holding_days:
                return ExitSignal(
                    trade.trade_id,
                    trade.symbol,
                    ExitReason.TIME_STOP,
                    price,
                    f"held {held_days:.1f}d, limit {self._max_holding_days}d",
                )

        return None

    # ------------------------------------------------------------ trailing

    def _trail(
        self, symbol: str, trade: TradeRecord, price: float, report: MonitorReport
    ) -> TradeRecord:
        """Raise the stop behind a winning long; at the target, hold if the trend holds.

        Runs before the exit checks, so a price that already fell back through the
        trailed level is caught by the stop check in the same sweep.
        """
        limits = self.profile.risk
        risk = trade.risk_per_share
        if risk <= 0:
            return trade

        peak = max(trade.peak_price or trade.entry_price, price)
        stop = trade.stop_price
        if peak >= trade.entry_price + limits.trail_activation_r * risk:
            stop = max(stop, trade.entry_price, peak - limits.trail_distance_r * risk)

        release_target = False
        if (
            limits.let_winners_run
            and trade.target_price is not None
            and price >= trade.target_price
            and self._trend_confirms(symbol, price)
        ):
            release_target = True
            half_target_gain = trade.entry_price + 0.5 * (trade.target_price - trade.entry_price)
            stop = max(stop, half_target_gain)

        if stop <= trade.stop_price and peak <= (trade.peak_price or 0) and not release_target:
            return trade

        updated = self._journal.trail_stop(
            trade.trade_id, stop, peak, release_target=release_target
        ) or trade
        if release_target:
            report.adjusted.append(
                f"{symbol} reached target {trade.target_price:.2f}, trend intact: holding"
            )
        if updated.stop_price > trade.stop_price:
            report.adjusted.append(
                f"{symbol} stop {trade.stop_price:.2f} -> {updated.stop_price:.2f}"
            )
            try:
                self._broker.update_stop(symbol, updated.stop_price)
            except Exception as exc:
                # Deliberately broad, as in the sweep: the journal stop is already
                # raised and this monitor enforces it every pass either way.
                report.errors.append(f"{symbol}: broker stop not moved ({exc})")
        return updated

    def _trend_confirms(self, symbol: str, price: float) -> bool:
        """Price above a rising short average. Missing data means take the profit."""
        try:
            bars = self._market_data.history(
                symbol,
                timeframe=self.profile.timeframe,
                limit=TREND_SMA_PERIOD + TREND_SLOPE_BARS,
            )
        except Exception:  # an outage here must fall back to the planned exit
            return False
        values = closes(bars)
        current = sma(values, TREND_SMA_PERIOD)
        earlier = sma(values[:-TREND_SLOPE_BARS], TREND_SMA_PERIOD)
        if current is None or earlier is None:
            return False
        return price > current and current > earlier

    # ------------------------------------------------------- close + reflect

    def _close(
        self,
        symbol: str,
        trades: list[TradeRecord],
        signal: ExitSignal,
        report: MonitorReport,
    ) -> None:
        """Exit at the broker, book the P&L, then hand the trade to Reflection.

        The reflection call is synchronous and inside this method on purpose. A trade
        that closes without producing a lesson is a trade the system learns nothing
        from, so the two steps are one unit of work rather than two schedules that can
        drift apart.
        """
        if signal.reason is ExitReason.BROKER_CLOSED:
            # Nothing to send: the position is already gone. Only the books are stale.
            exit_price = signal.price
            self.log.warning(
                "reconciling %s: %s (booking at last price %.2f)",
                symbol,
                signal.detail,
                exit_price,
            )
        else:
            outcome = self._execution.close(symbol, f"{signal.reason.value}: {signal.detail}")
            if not outcome.submitted:
                report.errors.append(f"{symbol}: broker did not close ({outcome.reason})")
                return
            exit_price = (
                outcome.result.filled_avg_price
                if outcome.result and outcome.result.filled_avg_price is not None
                else signal.price
            )

        # ExecutionAgent.close() flattens the whole symbol, and the Risk Manager
        # forbids pyramiding, so this list is normally one trade. If a legacy journal
        # holds several, they all closed on that one exit and are booked together.
        if len(trades) > 1:
            self.log.warning(
                "%d open journal trades on %s closed by a single broker exit",
                len(trades),
                symbol,
            )

        for trade in trades:
            closed = self._journal.close_trade(trade.trade_id, exit_price, self.clock.now())
            if closed is None:
                report.errors.append(
                    f"{symbol}: trade #{trade.trade_id} was already closed"
                )
                continue

            lessons = self._reflection.reflect_on_trade(closed, signal.reason)
            report.closed.append(
                ClosedTrade(
                    trade=closed,
                    reason=signal.reason,
                    exit_price=exit_price,
                    lessons=tuple(lessons),
                )
            )

    def close_manually(self, symbol: str, note: str = "") -> MonitorReport:
        """Operator-initiated exit that still books P&L and still writes a lesson."""
        now = self.clock.now()
        report = MonitorReport(client_id=self.client_id, checked_at=now)
        trades = [t for t in self._journal.open_trades() if t.symbol.upper() == symbol.upper()]
        report.open_trades = len(trades)
        if not trades:
            report.errors.append(f"{symbol}: no open position")
            return report

        price = self._market_data.last_price(symbol)
        signal = ExitSignal(
            trades[0].trade_id, symbol.upper(), ExitReason.MANUAL, price, note or "operator exit"
        )
        self._close(symbol.upper(), trades, signal, report)
        return report

    # ------------------------------------------------------------ poll loops

    def run(
        self,
        interval_seconds: float = 60.0,
        *,
        iterations: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> list[MonitorReport]:
        """Poll on an interval until `iterations` is reached or `stop_event` is set.

        Blocking. Use it as the body of a dedicated monitor process, or call
        `check_once()` yourself from an existing loop.
        """
        stop_event = stop_event or threading.Event()
        reports: list[MonitorReport] = []
        count = 0
        while not stop_event.is_set():
            reports.append(self.check_once())
            count += 1
            if iterations is not None and count >= iterations:
                break
            if stop_event.wait(interval_seconds):
                break
        return reports

    def start_background(
        self, interval_seconds: float = 60.0
    ) -> tuple[threading.Thread, threading.Event]:
        """Run the sweep on a daemon thread; returns the thread and its stop event.

        Safe because Journal holds its SQLite connection under a lock in WAL mode.
        Always set the event and join the thread before closing the session.
        """
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self.run,
            kwargs={"interval_seconds": interval_seconds, "stop_event": stop_event},
            name=f"monitor-{self.client_id}",
            daemon=True,
        )
        thread.start()
        return thread, stop_event

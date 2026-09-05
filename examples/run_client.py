"""End-to-end demonstration of one tenant's loop: entry, monitored exit, reflection.

Run from the project root:

    python examples/run_client.py

Uses the paper broker and synthetic market data, so it needs no credentials and no
network access. The DemoMarketData wrapper below walks prices between ticks so the
PositionMonitor actually fires — the library's SyntheticMarketData is deterministic
and would never move a position into its stop or target.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tradesys.core.config import Settings
from tradesys.core.models import Bar
from tradesys.data.market_data import SyntheticMarketData
from tradesys.orchestration.engine import TradingSystem

ROOT = Path(__file__).resolve().parent.parent


class DemoMarketData:
    """Demo-only: replays the synthetic series with a drift applied to live prices.

    Bars (used by the Scout) stay stable; only `last_price` moves, which is what the
    PositionMonitor polls. Call `drift(pct)` to push open positions toward a stop or
    a target on the next sweep.
    """

    def __init__(self) -> None:
        self._inner = SyntheticMarketData()
        self._multiplier = 1.0

    def drift(self, pct: float) -> None:
        self._multiplier *= 1.0 + pct / 100.0

    def history(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        return self._inner.history(symbol, timeframe=timeframe, limit=limit)

    def last_price(self, symbol: str) -> float:
        return round(self._inner.last_price(symbol) * self._multiplier, 4)


def show(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def main() -> None:
    settings = Settings.from_env(ROOT)
    market = DemoMarketData()
    system = TradingSystem(settings, market_data=market)

    session = system.for_client("tal_dahan")
    show(f"{session.profile.display_name} — isolated stack")
    print(f"watchlist    : {', '.join(session.profile.symbols)}")
    print(f"max drawdown : {session.profile.risk.max_drawdown_pct}%")
    print(f"time stop    : {session.profile.max_holding_days} days")
    print(f"memory space : {session.tenant.memory_collection}")

    # ---- 1. a cycle that opens positions ------------------------------------
    show("cycle 1 — scout, risk, execute")
    first = session.run_cycle()
    print(first.summary())
    for outcome in first.outcomes:
        print(f"  {outcome.symbol}: submitted={outcome.submitted} trade={outcome.trade_id}")

    for trade in session.journal.open_trades():
        target = f"{trade.target_price:.2f}" if trade.target_price else "none"
        print(
            f"  open #{trade.trade_id} {trade.symbol} x{trade.qty} @ "
            f"{trade.entry_price:.2f}  stop {trade.stop_price:.2f}  target {target}"
        )

    # ---- 2. the monitor, driven manually one sweep at a time -----------------
    # This is the injection point: check_once() is a pure function of current price
    # and open positions, so you can call it from any loop you already have.
    show("monitor sweeps — nothing has moved yet")
    print(session.monitor.check_once().summary())

    show("monitor sweep — after a 15% drawdown, stops should trigger")
    market.drift(-15.0)
    report = session.monitor.check_once()
    print(report.summary())
    for closed in report.closed:
        print(
            f"  #{closed.trade.trade_id} {closed.trade.symbol}: {closed.reason.value} "
            f"@ {closed.exit_price:.2f}  pnl {closed.trade.realized_pnl:+.2f} "
            f"({closed.trade.r_multiple():+.2f}R)"
        )
        for lesson in closed.lessons:
            print(f"    -> memory[{lesson.kind.value}]: {lesson.text}")

    # ---- 3. the lesson is already retrievable by the Scout -------------------
    show("what the Scout will retrieve on its next scan")
    for text in session.reflection.recall("stop loss on a breakout", k=3):
        print(f"  - {text}")

    # ---- 4. the same monitor, running itself on an interval ------------------
    # Equivalent to `tradesys monitor --client tal_dahan`. Runs in-thread; use
    # start_background() to hand it a daemon thread instead.
    show("monitor.run() — two timed sweeps")
    for sweep in session.monitor.run(interval_seconds=1.0, iterations=2):
        print(f"  {sweep.summary()}")

    # ---- 5. isolation still holds -------------------------------------------
    other = system.for_client("client_001")
    show("second tenant, same process")
    print(f"memory space : {other.tenant.memory_collection}")
    print(f"its recall of 'stop loss': {other.reflection.recall('stop loss', k=3)}")

    session.close()
    other.close()


if __name__ == "__main__":
    main()

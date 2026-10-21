"""Historical replay: the live agent stack, day by day, over real past prices.

Nothing here re-implements a strategy. The same Scout, Offense, Risk Manager,
Execution, Monitor and Reflection that trade live run against a market that only
reveals bars up to the simulated day, so no decision can see the future.

Assumptions, stated in every result because they flatter or hurt the numbers:
  * Entries fill at the signal day's close.
  * Intraday exits use the day's range, stop first: a bar that touched both the
    stop and the target is booked as the stop. A gap through the stop fills at
    the open, not at the stop.
  * A trailed stop raised on a day's high and breached by the same day's close
    exits at the close.
  * Earnings gaps are not modeled (no point-in-time earnings history), and there
    is no slippage or commission.
  * Drawdown halts are recorded, then released the next day with the peak
    rebased, as an operator would; the count of halts is part of the result.

Run: python -m tradesys.backtest --client tal_dahan --years 3
"""

from __future__ import annotations

import argparse
import bisect
import json
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Sequence

from tradesys.clients.profile import BrokerConfig, ClientProfile
from tradesys.core.clock import FrozenClock
from tradesys.core.config import Settings
from tradesys.core.models import Bar, TradeRecord
from tradesys.data.earnings import EarningsLookup
from tradesys.data.market_data import MarketDataProvider

BENCHMARK = "SPY"
WARMUP_BARS = 260          # enough history for the Scout and the 200-bar regime average
ASSUMPTIONS = (
    "כניסות במחיר הסגירה של יום האות",
    "יציאות לפי טווח היום, סטופ קודם: נר שנגע גם בסטופ וגם ביעד נרשם כסטופ",
    "פער מחיר מעבר לסטופ מתמלא במחיר הפתיחה",
    "קפיצות מחיר של דוחות לא מדומות, ואין עמלות או החלקה",
    "עצירות מסחר נרשמות ומשוחררות למחרת, כמו שמנהל היה עושה",
)


class ReplayMarketData:
    """A market that reveals bars only up to `today`, with per-pass price overrides."""

    def __init__(self, bars: dict[str, list[Bar]]) -> None:
        self._bars = {s: sorted(b, key=lambda x: x.ts) for s, b in bars.items()}
        self._dates = {s: [b.ts.date() for b in series] for s, series in self._bars.items()}
        self.today: date | None = None
        self.prices: dict[str, float] = {}

    def set_day(self, day: date) -> None:
        self.today = day
        self.prices = {}

    def bar(self, symbol: str) -> Bar | None:
        series = self.history(symbol, timeframe="1Day", limit=1)
        return series[0] if series and series[0].ts.date() == self.today else None

    def history(self, symbol: str, *, timeframe: str, limit: int) -> list[Bar]:
        del timeframe
        series = self._bars.get(symbol.upper(), [])
        end = bisect.bisect_right(self._dates.get(symbol.upper(), []), self.today)
        return series[max(0, end - limit):end]

    def last_price(self, symbol: str) -> float:
        if symbol.upper() in self.prices:
            return self.prices[symbol.upper()]
        series = self.history(symbol, timeframe="1Day", limit=1)
        if not series:
            raise ValueError(f"no replay data for {symbol} on {self.today}")
        return series[-1].close


class _ClearCalendar:
    """No point-in-time earnings history exists here; treat every name as clear."""

    def lookup(self, symbol: str, on_or_after: date) -> EarningsLookup:
        return EarningsLookup.none_scheduled()


class _OneProfile:
    def __init__(self, profile: ClientProfile) -> None:
        self._profile = profile

    def load(self, client_id: str, *, refresh: bool = False) -> ClientProfile:
        return self._profile

    def list_ids(self) -> list[str]:
        return [self._profile.client_id]


@dataclass(slots=True)
class BacktestResult:
    client_id: str
    start: str
    end: str
    starting_equity: float
    final_equity: float
    total_return_pct: float
    benchmark_return_pct: float
    max_drawdown_pct: float
    trades: int
    win_rate: float
    avg_r: float
    profit_factor: float | None
    halts: list[str] = field(default_factory=list)
    by_setup: list[dict] = field(default_factory=list)
    by_symbol: list[dict] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    benchmark_curve: list[tuple[str, float]] = field(default_factory=list)
    assumptions: tuple[str, ...] = ASSUMPTIONS


def fetch_bars(
    source: MarketDataProvider,
    symbols: Sequence[str],
    years: int,
    cache_dir: Path | None = None,
) -> dict[str, list[Bar]]:
    """Daily bars for every symbol plus the benchmark, cached per symbol per day.

    A universe of a hundred names is a hundred requests; the cache makes a second
    run the same day free, and it is keyed by date so it never serves stale bars.
    """
    limit = years * 252 + WARMUP_BARS
    wanted = sorted({s.upper() for s in symbols} | {BENCHMARK})
    today = date.today().isoformat()
    bars: dict[str, list[Bar]] = {}
    for symbol in wanted:
        cached = cache_dir / f"{symbol}_{today}_{limit}.json" if cache_dir else None
        if cached is not None and cached.exists():
            raw = json.loads(cached.read_text(encoding="utf-8"))
            bars[symbol] = [
                Bar(datetime.fromisoformat(b["ts"]), b["o"], b["h"], b["l"], b["c"], b["v"])
                for b in raw
            ]
            continue
        bars[symbol] = source.history(symbol, timeframe="1Day", limit=limit)
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(
                json.dumps([
                    {"ts": b.ts.isoformat(), "o": b.open, "h": b.high, "l": b.low,
                     "c": b.close, "v": b.volume}
                    for b in bars[symbol]
                ]),
                encoding="utf-8",
            )
    return bars


def _set_prices(market: ReplayMarketData, trades: Sequence[TradeRecord], pick) -> None:
    for trade in trades:
        bar = market.bar(trade.symbol)
        if bar is not None:
            market.prices[trade.symbol.upper()] = pick(trade, bar)


def _stop_pass(trade: TradeRecord, bar: Bar) -> float:
    # A gap through the stop fills at the open; otherwise at the stop itself.
    return min(bar.open, trade.stop_price) if bar.low <= trade.stop_price else bar.low


def _high_pass(trade: TradeRecord, bar: Bar) -> float:
    if trade.target_price is not None and bar.high >= trade.target_price:
        return max(bar.open, trade.target_price)
    return bar.high


def _stats(trades: Sequence[TradeRecord], key) -> list[dict]:
    groups: dict[str, list[TradeRecord]] = {}
    for trade in trades:
        groups.setdefault(key(trade), []).append(trade)
    rows = []
    for name, group in sorted(groups.items()):
        rs = [t.r_multiple() for t in group]
        rows.append({
            "name": name,
            "trades": len(group),
            "win_rate": sum(1 for t in group if (t.realized_pnl or 0) > 0) / len(group),
            "avg_r": sum(rs) / len(rs),
            "pnl": sum(t.realized_pnl or 0 for t in group),
        })
    return rows


def _max_drawdown(curve: Sequence[tuple[str, float]]) -> float:
    peak, worst = 0.0, 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        if peak:
            worst = max(worst, (peak - equity) / peak * 100)
    return worst


def run_backtest(
    profile: ClientProfile,
    bars: dict[str, list[Bar]],
    years: int,
    workdir: Path,
) -> BacktestResult:
    from tradesys.orchestration.engine import TradingSystem

    # Always simulated money, whatever broker the live profile uses.
    profile = replace(profile, broker=BrokerConfig(name="paper", paper=True))
    market = ReplayMarketData(bars)
    days = [b.ts.date() for b in bars[BENCHMARK]][-years * 252:]
    clock = FrozenClock(datetime.combine(days[0], time(20, 0), timezone.utc))
    system = TradingSystem(
        Settings(clients_dir=workdir, data_dir=workdir, log_level="WARNING"),
        registry=_OneProfile(profile),
        market_data=market,
        clock=clock,
        earnings=_ClearCalendar(),
    )
    session = system.for_client(profile.client_id)
    halts: list[str] = []
    curve: list[tuple[str, float]] = []
    try:
        for day in days:
            clock.set(datetime.combine(day, time(20, 0), timezone.utc))
            market.set_day(day)

            halted, reason = session.journal.halt_state()
            if halted:
                equity = session.broker.account().equity
                halts.append(f"{day.isoformat()}: {reason}")
                session.journal.clear_halt(rebase_peak_to=equity)

            open_trades = session.journal.open_trades()
            if open_trades:
                _set_prices(market, open_trades, _stop_pass)
                session.monitor.check_once()
                _set_prices(market, session.journal.open_trades(), _high_pass)
                session.monitor.check_once()
                market.prices = {}

            session.run_cycle()
            curve.append((day.isoformat(), session.broker.account().equity))
        closed = session.journal.closed_trades(limit=100_000)
    finally:
        session.close()

    start_equity = curve[0][1] if curve else 0.0
    final_equity = curve[-1][1] if curve else 0.0
    spy = {b.ts.date().isoformat(): b.close for b in bars[BENCHMARK]}
    first_spy = spy[days[0].isoformat()]
    bench_curve = [(d, start_equity * spy[d] / first_spy) for d, _ in curve if d in spy]
    gains = sum(t.realized_pnl or 0 for t in closed if (t.realized_pnl or 0) > 0)
    losses = -sum(t.realized_pnl or 0 for t in closed if (t.realized_pnl or 0) < 0)
    rs = [t.r_multiple() for t in closed]
    return BacktestResult(
        client_id=profile.client_id,
        start=days[0].isoformat(),
        end=days[-1].isoformat(),
        starting_equity=start_equity,
        final_equity=final_equity,
        total_return_pct=(final_equity / start_equity - 1) * 100 if start_equity else 0.0,
        benchmark_return_pct=(spy[days[-1].isoformat()] / first_spy - 1) * 100,
        max_drawdown_pct=_max_drawdown(curve),
        trades=len(closed),
        win_rate=sum(1 for t in closed if (t.realized_pnl or 0) > 0) / len(closed) if closed else 0.0,
        avg_r=sum(rs) / len(rs) if rs else 0.0,
        profit_factor=gains / losses if losses else None,
        halts=halts,
        by_setup=_stats(closed, lambda t: t.setup.value),
        by_symbol=_stats(closed, lambda t: t.symbol),
        equity_curve=curve,
        benchmark_curve=bench_curve,
    )


def save_result(result: BacktestResult, data_dir: Path) -> Path:
    folder = data_dir / "_platform" / "backtests"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{result.client_id}.json"
    payload = asdict(result)
    payload["ran_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load_result(data_dir: Path, client_id: str) -> dict | None:
    path = data_dir / "_platform" / "backtests" / f"{client_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def backtest_client(client_id: str, years: int, settings: Settings) -> BacktestResult:
    """Fetch real history for the client's watchlist and replay it in a scratch dir."""
    from tradesys.data.market_data import AlpacaMarketData
    from tradesys.orchestration.engine import TradingSystem

    if not (settings.market_data_key_id and settings.market_data_secret_key):
        raise RuntimeError("a historical test needs the Alpaca market-data keys")
    profile = TradingSystem(settings).registry.load(client_id)
    source = AlpacaMarketData(settings.market_data_key_id, settings.market_data_secret_key)
    bars = fetch_bars(source, profile.symbols, years, settings.data_dir / "_platform" / "bars_cache")
    with tempfile.TemporaryDirectory() as scratch:
        return run_backtest(profile, bars, years, Path(scratch))


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a client's agents over past prices")
    parser.add_argument("--client", required=True)
    parser.add_argument("--years", type=int, default=3)
    args = parser.parse_args()
    settings = Settings.from_env()
    result = backtest_client(args.client, args.years, settings)
    path = save_result(result, settings.data_dir)
    print(
        f"{result.start} -> {result.end}: {result.trades} trades, "
        f"return {result.total_return_pct:+.2f}% vs SPY {result.benchmark_return_pct:+.2f}%, "
        f"max drawdown {result.max_drawdown_pct:.2f}%, halts {len(result.halts)}\n"
        f"saved {path}"
    )


if __name__ == "__main__":
    main()

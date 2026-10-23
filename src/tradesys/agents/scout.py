from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from tradesys.agents.base import Agent
from tradesys.clients.profile import ClientProfile
from tradesys.core.clock import Clock
from tradesys.core.enums import MemoryKind, SetupType, Side
from tradesys.core.models import Bar, Candidate, FeedbackDirective
from tradesys.core.tenancy import TenantContext
from tradesys.data import indicators
from tradesys.data.market_data import MarketDataProvider
from tradesys.memory.directives import apply_directives
from tradesys.memory.store import VectorMemory

TARGET_R_MULTIPLE = 2.5


@dataclass(frozen=True, slots=True)
class Signal:
    setup: SetupType
    confidence: float
    rationale: str


def detect_setups(bars: Sequence[Bar]) -> tuple[list[Signal], float, dict[str, float]] | None:
    """The reference setups on the last bar, with its ATR and features.

    These are structural placeholders that show where signal logic plugs in.
    They are not a validated edge — replace them with your own researched rules
    and backtest before trusting them with capital. The strategy lab replays
    exactly this function, so what it tests is what the robot trades.
    """
    prices = indicators.closes(bars)
    last = bars[-1]
    atr = indicators.atr(bars, 14)
    if not atr or atr <= 0:
        return None

    sma20 = indicators.sma(prices, 20)
    sma50 = indicators.sma(prices, 50)
    rsi14 = indicators.rsi(prices, 14)
    avg_volume = indicators.average_volume(bars, 20) or 0.0
    breakout_level = indicators.donchian_high(bars, 20)
    if sma20 is None or sma50 is None or rsi14 is None or breakout_level is None:
        return None

    features = {
        "atr": atr,
        "rsi": rsi14,
        "sma20": sma20,
        "sma50": sma50,
        "avg_volume": avg_volume,
        "volume_ratio": last.volume / avg_volume if avg_volume else 0.0,
    }

    found: list[Signal] = []
    if (
        last.close > breakout_level
        and avg_volume > 0
        and last.volume > 1.5 * avg_volume
        and last.close > sma50
    ):
        found.append(Signal(
            SetupType.BREAKOUT, 0.62,
            f"closed {last.close:.2f} above the 20-bar high "
            f"{breakout_level:.2f} on {features['volume_ratio']:.1f}x volume",
        ))
    if last.close > sma50 and sma20 <= last.close <= sma20 * 1.02 and 40 <= rsi14 <= 58:
        found.append(Signal(
            SetupType.PULLBACK, 0.55,
            f"pullback to the 20SMA ({sma20:.2f}) while above the 50SMA, RSI {rsi14:.0f}",
        ))
    if rsi14 < 30 and last.close > bars[-2].close:
        found.append(Signal(
            SetupType.OVERSOLD_REVERSAL, 0.45, f"RSI {rsi14:.0f} with an up-close reversal bar",
        ))
    if last.close > sma20 > sma50 and rsi14 > 55:
        found.append(Signal(
            SetupType.TREND_CONTINUATION, 0.50,
            f"price above a rising 20/50 SMA stack, RSI {rsi14:.0f}",
        ))
    return found, atr, features


class ScoutAgent(Agent):
    """Proposes trade ideas from one tenant's watchlist, informed by its own memory.

    The pipeline is deliberately split in two:

      1. *Detection* is mechanical (indicators only). It produces candidates with
         concrete entry, stop and target levels, so a downstream model cannot invent
         price levels the risk engine would have to trust.
      2. *Ranking* is where memory enters. Retrieved lessons adjust confidence, and
         AVOID directives veto candidates outright — in code, not in a prompt.
    """

    name = "scout"

    def __init__(
        self,
        tenant: TenantContext,
        profile: ClientProfile,
        market_data: MarketDataProvider,
        memory: VectorMemory,
        directives: Sequence[FeedbackDirective] = (),
        clock: Clock | None = None,
    ) -> None:
        super().__init__(tenant, profile, clock)
        if memory.client_id != tenant.client_id:
            raise ValueError("memory store belongs to a different tenant")
        self._market_data = market_data
        self._memory = memory
        self._directives = tuple(directives)

    def scan(self, focus: set[str] | None = None) -> list[Candidate]:
        """Ideas from the watchlist, or only from `focus` when the hunter set one."""
        raw: list[Candidate] = []
        for entry in self.profile.watchlist:
            if focus is not None and entry.symbol not in focus:
                continue
            bars = self._market_data.history(
                entry.symbol,
                timeframe=self.profile.timeframe,
                limit=self.profile.lookback_bars,
            )
            if len(bars) < 60:
                self.log.debug("skipping %s: only %d bars", entry.symbol, len(bars))
                continue
            raw.extend(self._detect(entry.symbol, bars))

        ranked = [c for c in (self._apply_memory(c) for c in raw) if c is not None]
        # The client's priority picks survive the per-cycle cut, then confidence.
        priority = set(self.profile.priority_symbols)
        ranked.sort(key=lambda c: (c.symbol not in priority, -c.confidence))
        return ranked[: self.profile.max_candidates_per_cycle]

    # ------------------------------------------------------------- detection

    def _detect(self, symbol: str, bars: Sequence[Bar]) -> list[Candidate]:
        detected = detect_setups(bars)
        if detected is None:
            return []
        signals, atr, features = detected
        return [
            self._build(
                symbol, signal.setup, bars[-1].close, atr, features,
                confidence=signal.confidence, rationale=signal.rationale,
            )
            for signal in signals
            if self.profile.allows_setup(symbol, signal.setup)
        ]

    def _build(
        self,
        symbol: str,
        setup: SetupType,
        price: float,
        atr: float,
        features: dict[str, float],
        *,
        confidence: float,
        rationale: str,
    ) -> Candidate:
        stop = price - self.profile.stop_multiple(symbol) * atr
        target = price + TARGET_R_MULTIPLE * (price - stop)
        return Candidate(
            client_id=self.client_id,
            symbol=symbol,
            setup=setup,
            side=Side.BUY,
            entry=round(price, 4),
            stop=round(stop, 4),
            target=round(target, 4),
            confidence=confidence,
            rationale=rationale,
            features=features,
        )

    # ---------------------------------------------------------------- memory

    def _memory_query(self, candidate: Candidate) -> str:
        return (
            f"{candidate.symbol} {candidate.setup.value} setup, "
            f"entry {candidate.entry:.2f}, {candidate.rationale}"
        )

    def _apply_memory(self, candidate: Candidate) -> Candidate | None:
        """RAG step: retrieve this tenant's lessons, then enforce its directives.

        Returns ``None`` when a directive vetoes the idea, so it never reaches the
        Risk Manager or the broker.
        """
        vetoed, multiplier, notes = apply_directives(self._directives, candidate)
        if vetoed:
            self.log.info("memory veto on %s: %s", candidate.symbol, "; ".join(notes))
            return None

        hits = self._memory.query(
            self._memory_query(candidate),
            k=self.profile.memory_top_k,
            kinds=(
                MemoryKind.HUMAN_FEEDBACK,
                MemoryKind.TRADE_POSTMORTEM,
                MemoryKind.SESSION_LESSON,
            ),
        )
        context = tuple(
            f"[{hit.score:.2f}] {hit.record.text}" for hit in hits if hit.score > 0.15
        )

        return replace(
            candidate,
            confidence=round(min(candidate.confidence * multiplier, 0.99), 4),
            memory_context=context + tuple(notes),
        )

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from tradesys.agents.base import Agent
from tradesys.clients.profile import ClientProfile
from tradesys.core.clock import Clock
from tradesys.core.enums import ExitReason, MemoryKind, SetupType
from tradesys.core.models import FeedbackDirective, MemoryRecord, TradeRecord
from tradesys.core.tenancy import TenantContext
from tradesys.memory.directives import DirectiveExtractor, RuleBasedDirectiveExtractor
from tradesys.memory.store import VectorMemory
from tradesys.persistence.journal import Journal


@dataclass(frozen=True, slots=True)
class FeedbackReceipt:
    """What the operator gets back after submitting feedback."""

    client_id: str
    memory_id: str
    directives: tuple[FeedbackDirective, ...]
    trade_id: int | None = None

    def describe(self) -> str:
        if not self.directives:
            return "stored as context only"
        return "; ".join(
            f"{d.action.value} "
            f"{'/'.join(d.symbols) or '/'.join(s.value for s in d.setups) or 'global'}"
            for d in self.directives
        )


class ReflectionAgent(Agent):
    """The continuous-learning loop, and the only writer to tenant memory.

    Two inputs feed it:

      * **Machine reflection** — closed trades are summarised into postmortems, plus
        an aggregate lesson per setup once there is enough history to be meaningful.
      * **Human feedback** — free text from the operator, which is stored verbatim for
        retrieval *and* compiled into structured directives that bind the Scout.

    Storing both matters. The prose gives the next cycle nuance; the directives give
    it guarantees. Prose alone would make behaviour depend on prompt phrasing.
    """

    name = "reflection"

    _MIN_TRADES_FOR_SETUP_LESSON = 5

    def __init__(
        self,
        tenant: TenantContext,
        profile: ClientProfile,
        memory: VectorMemory,
        journal: Journal,
        extractor: DirectiveExtractor | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(tenant, profile, clock)
        if memory.client_id != tenant.client_id:
            raise ValueError("memory store belongs to a different tenant")
        self._memory = memory
        self._journal = journal
        self._extractor = extractor or RuleBasedDirectiveExtractor()

    # ------------------------------------------------------- human in the loop

    def ingest_feedback(
        self, text: str, trade_id: int | None = None, *, source: str = "human"
    ) -> FeedbackReceipt:
        """Accept one operator instruction and make it binding from the next cycle on.

        `source` tells the client's own notes ("human") from the platform
        operator's ("operator"), so the client's journal can show who wrote what.

        Example inputs:
            "Trade #45 on RIVN was great, prioritize this setup"
            "Adjust strategy to avoid buying breakouts"
        """
        text = text.strip()
        if not text:
            raise ValueError("feedback text is empty")

        trade = self._journal.get_trade(trade_id) if trade_id is not None else None
        if trade_id is not None and trade is None:
            raise ValueError(f"trade #{trade_id} not found for client {self.client_id}")

        directives = self._extractor.extract(
            self.client_id,
            text,
            known_symbols=self.profile.symbols,
            trade_id=trade_id,
        )
        # An LLM extractor may return a symbol the operator never mentioned; the
        # tenant's own watchlist is the closed vocabulary, enforced here as well.
        directives = [
            d
            for d in directives
            if all(symbol in self.profile.symbols for symbol in d.symbols)
        ]
        directive_ids = self._journal.save_directives(directives)
        stored = tuple(
            replace(d, directive_id=directive_id)
            for d, directive_id in zip(directives, directive_ids)
        )

        record = MemoryRecord(
            client_id=self.client_id,
            kind=MemoryKind.HUMAN_FEEDBACK,
            text=self._render_feedback(text, trade),
            metadata={
                "source": source,
                "trade_id": trade_id,
                "symbol": trade.symbol if trade else "",
                "setup": trade.setup.value if trade else "",
                "directive_ids": ",".join(str(i) for i in directive_ids),
            },
            created_at=self.clock.now(),
        )
        memory_id = self._memory.add([record])[0]
        self.log.info(
            "ingested feedback (%d directive(s)): %s", len(stored), text[:120]
        )
        return FeedbackReceipt(
            client_id=self.client_id,
            memory_id=memory_id,
            directives=stored,
            trade_id=trade_id,
        )

    def _render_feedback(self, text: str, trade: TradeRecord | None) -> str:
        if trade is None:
            return f"Operator guidance: {text}"
        return (
            f"Operator guidance on trade #{trade.trade_id} "
            f"({trade.symbol} {trade.setup.value}, "
            f"pnl {trade.realized_pnl if trade.realized_pnl is not None else 0.0:.2f}): "
            f"{text}"
        )

    def active_directives(self) -> list[FeedbackDirective]:
        return self._journal.active_directives()

    def retire_directive(self, directive_id: int) -> bool:
        """Operators change their minds; a directive can be withdrawn without a rewrite."""
        return self._journal.retire_directive(directive_id)

    # ---------------------------------------------------- machine reflection

    def reflect_on_trade(
        self, trade: TradeRecord, exit_reason: ExitReason | None = None
    ) -> list[MemoryRecord]:
        """Reflect on one trade the instant it closes.

        Called synchronously by the PositionMonitor so the lesson is in the tenant's
        vector namespace before the Scout's next scan — a trade that stops out at
        10:05 informs the 10:06 cycle, not tomorrow's batch job.

        Idempotent: a trade already marked reflected produces nothing, so a retry or
        an overlapping batch run cannot write the same postmortem twice.
        """
        self.tenant.assert_owns(trade)
        if not trade.is_closed:
            raise ValueError(f"trade #{trade.trade_id} is still open")
        if trade.trade_id not in {
            t.trade_id for t in self._journal.unreflected_closed_trades(limit=1000)
        }:
            return []

        records = [self._postmortem(trade, exit_reason)]
        records.extend(self._setup_lessons(trade.setup))
        self._memory.add(records)
        self._journal.mark_reflected([trade.trade_id])
        self.log.info(
            "reflected on trade #%d %s (%s, %+.2f)",
            trade.trade_id,
            trade.symbol,
            exit_reason.value if exit_reason else "closed",
            trade.realized_pnl or 0.0,
        )
        return records

    def reflect(self, limit: int = 50) -> list[MemoryRecord]:
        """Batch sweep for trades closed outside the monitor (manual exits, restarts)."""
        trades = self._journal.unreflected_closed_trades(limit)
        if not trades:
            return []

        records = [self._postmortem(trade, None) for trade in trades]
        for setup in {t.setup for t in trades}:
            records.extend(self._setup_lessons(setup))
        self._memory.add(records)
        self._journal.mark_reflected(t.trade_id for t in trades)
        self.log.info("reflected on %d closed trade(s)", len(trades))
        return records

    _VERDICTS = {
        ExitReason.TAKE_PROFIT: "thesis confirmed — the target was reached",
        ExitReason.STOP_LOSS: "thesis invalidated — price hit the stop first",
        ExitReason.TIME_STOP: "thesis stalled — neither target nor stop was reached in time",
        ExitReason.BROKER_CLOSED: "closed at the broker outside the monitor's control",
        ExitReason.MANUAL: "closed manually by the operator",
    }

    def _postmortem(
        self, trade: TradeRecord, exit_reason: ExitReason | None
    ) -> MemoryRecord:
        pnl = trade.realized_pnl or 0.0
        outcome = "win" if pnl > 0 else "loss"
        r_multiple = trade.r_multiple()
        held = trade.holding_period(trade.closed_at or self.clock.now())
        held_days = held.total_seconds() / 86400
        verdict = self._VERDICTS.get(exit_reason, "closed without a recorded trigger")

        text = (
            f"Trade #{trade.trade_id} {trade.symbol} {trade.setup.value} "
            f"({trade.side.value} {trade.qty} @ {trade.entry_price:.2f}, "
            f"stop {trade.stop_price:.2f}"
            + (f", target {trade.target_price:.2f}" if trade.target_price else "")
            + f", exit {trade.exit_price:.2f} after {held_days:.1f}d) "
            f"was a {outcome}: {pnl:+.2f} ({r_multiple:+.2f}R). "
            f"Entry thesis: {trade.rationale or 'not recorded'}. "
            f"Verdict: {verdict}."
        )
        return MemoryRecord(
            client_id=self.client_id,
            kind=MemoryKind.TRADE_POSTMORTEM,
            text=text,
            metadata={
                "trade_id": trade.trade_id,
                "symbol": trade.symbol,
                "setup": trade.setup.value,
                "outcome": outcome,
                "exit_reason": exit_reason.value if exit_reason else "",
                "r_multiple": round(r_multiple, 3),
                "holding_days": round(held_days, 3),
            },
            created_at=self.clock.now(),
        )

    def _setup_lessons(self, setup: SetupType) -> list[MemoryRecord]:
        """Aggregate statistics beat single-trade anecdotes, so store them separately.

        Emitted only when the sample crosses a multiple of the minimum, otherwise every
        close would append a near-duplicate aggregate and dilute retrieval.
        """
        row = next(
            (r for r in self._journal.performance_by_setup() if r["setup"] == setup.value),
            None,
        )
        if row is None or row["n"] < self._MIN_TRADES_FOR_SETUP_LESSON:
            return []
        if row["n"] % self._MIN_TRADES_FOR_SETUP_LESSON != 0:
            return []

        win_rate = row["wins"] / row["n"] * 100
        return [
            MemoryRecord(
                client_id=self.client_id,
                kind=MemoryKind.SESSION_LESSON,
                text=(
                    f"Across {row['n']} closed {row['setup']} trades this account "
                    f"won {win_rate:.0f}% for {row['pnl']:+.2f} total."
                ),
                metadata={"setup": row["setup"], "sample_size": row["n"]},
                created_at=self.clock.now(),
            )
        ]

    def recall(self, query: str, k: int = 5) -> Sequence[str]:
        return [hit.record.text for hit in self._memory.query(query, k=k)]

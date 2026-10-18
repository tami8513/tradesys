from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from tradesys.agents.base import Agent
from tradesys.clients.profile import ClientProfile
from tradesys.core.clock import Clock
from tradesys.core.models import Candidate
from tradesys.core.tenancy import TenantContext
from tradesys.persistence.journal import Journal

PROVEN_TRADES = 8
PROVEN_EDGE_R = 0.3
CONVICTION = {"proven": 1.0, "unproven": 0.75, "losing": 0.5}


@dataclass(frozen=True, slots=True)
class SetupEdge:
    trades: int
    expectancy: float   # mean R-multiple of this tenant's closed trades in the setup

    @property
    def verdict(self) -> str:
        if self.trades < PROVEN_TRADES:
            return "unproven"
        if self.expectancy >= PROVEN_EDGE_R:
            return "proven"
        return "losing" if self.expectancy < 0 else "unproven"


class OffenseAgent(Agent):
    """Puts the risk budget where this tenant's own record says the edge is.

    It never widens risk. The client's risk level sets the per-trade ceiling and
    the Risk Manager enforces it; this agent only chooses how much of that ceiling
    each idea gets, and which ideas take the day's limited slots first. A setup
    earns full size by proving itself here, over enough trades, not by looking good.
    """

    name = "offense"

    def __init__(
        self,
        tenant: TenantContext,
        profile: ClientProfile,
        journal: Journal,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(tenant, profile, clock)
        self._journal = journal

    def edges(self) -> dict[str, SetupEdge]:
        by_setup: dict[str, list[float]] = {}
        for trade in self._journal.closed_trades(limit=500):
            by_setup.setdefault(trade.setup.value, []).append(trade.r_multiple())
        return {
            setup: SetupEdge(len(rs), sum(rs) / len(rs)) for setup, rs in by_setup.items()
        }

    def plan(self, candidates: Sequence[Candidate]) -> list[tuple[Candidate, float]]:
        """Candidates strongest-first, each with its share of the risk ceiling."""
        edges = self.edges()

        def edge_of(candidate: Candidate) -> SetupEdge:
            return edges.get(candidate.setup.value, SetupEdge(0, 0.0))

        def score(candidate: Candidate) -> float:
            edge = edge_of(candidate)
            if edge.verdict == "unproven":
                return candidate.confidence
            return candidate.confidence + 0.2 * max(-1.0, min(1.0, edge.expectancy))

        ranked = sorted(candidates, key=score, reverse=True)
        return [(c, CONVICTION[edge_of(c).verdict]) for c in ranked]

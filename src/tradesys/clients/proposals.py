"""Trades the robot wants to make in an "ask first" pick, waiting for the client.

A proposal is not an order. It records the idea as the Risk Manager approved it,
and lives until the end of that trading day. Approving it does not replay the old
decision: the session re-prices the idea at the live trade and runs the full
Risk Manager again, because the market and the book may have moved since.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from tradesys.core.fileio import locked, write_json_atomic
from tradesys.core.models import Candidate

PROPOSALS_FILE = "proposals.json"


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    symbol: str
    setup: str
    entry: float
    stop: float
    target: float
    qty: int
    confidence: float
    rationale: str
    avg_volume: float
    created_at: str
    expires_at: str              # end of the trading day it was made on
    status: str = "pending"      # pending | approved | declined | blocked | expired
    note: str = ""               # what happened on approval, in Hebrew

    def expired(self, now: datetime) -> bool:
        return now >= datetime.fromisoformat(self.expires_at)


def _path(tenant_dir: Path) -> Path:
    return Path(tenant_dir) / PROPOSALS_FILE


def read_proposals(tenant_dir: Path) -> list[Proposal]:
    try:
        raw = json.loads(_path(tenant_dir).read_text(encoding="utf-8"))
        return [Proposal(**item) for item in raw]
    except (OSError, ValueError, TypeError):
        return []


def pending(tenant_dir: Path, now: datetime) -> list[Proposal]:
    return [p for p in read_proposals(tenant_dir) if p.status == "pending" and not p.expired(now)]


def propose(
    tenant_dir: Path, candidate: Candidate, qty: int, now: datetime, expires_at: datetime
) -> Proposal | None:
    """Record a proposal, unless one for this stock is already waiting."""
    path = _path(tenant_dir)
    with locked(path):
        proposals = read_proposals(tenant_dir)
        if any(p.symbol == candidate.symbol and p.status == "pending" and not p.expired(now)
               for p in proposals):
            return None
        proposal = Proposal(
            proposal_id=uuid.uuid4().hex[:12],
            symbol=candidate.symbol,
            setup=candidate.setup.value,
            entry=candidate.entry,
            stop=candidate.stop,
            target=candidate.target,
            qty=qty,
            confidence=candidate.confidence,
            rationale=candidate.rationale,
            avg_volume=float(candidate.features.get("avg_volume", 0.0)),
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
        )
        # Keep the last few weeks only; the journal holds the lasting record.
        write_json_atomic(path, [asdict(p) for p in [*proposals, proposal][-100:]])
        return proposal


def resolve(tenant_dir: Path, proposal_id: str, status: str, note: str = "") -> Proposal | None:
    path = _path(tenant_dir)
    with locked(path):
        proposals = read_proposals(tenant_dir)
        for index, proposal in enumerate(proposals):
            if proposal.proposal_id == proposal_id:
                proposals[index] = replace(proposal, status=status, note=note)
                write_json_atomic(path, [asdict(p) for p in proposals])
                return proposals[index]
    return None

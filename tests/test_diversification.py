from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_account, make_candidate, make_profile, make_stats

from tradesys.agents.risk_manager import RiskManager
from tradesys.core.models import Bar, Position
from tradesys.core.tenancy import TenantContext
from tradesys.data.indicators import return_correlation
from tradesys.data.sectors import sector_label, sector_of
from tradesys.orchestration.why import explain_reason
from tradesys.persistence.journal import Journal

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def manager(tmp_path, clock, symbols=("NVDA", "AMD", "JPM"), **risk):
    base = make_profile(symbols=symbols)
    profile = replace(base, risk=replace(base.risk, **risk))
    tenant = TenantContext.create(profile.client_id, tmp_path)
    return RiskManager(tenant, profile, Journal(tenant), clock)


def held(symbol: str, value: float) -> Position:
    return Position("tal_dahan", symbol, int(value // 100), 100.0, 100.0)


def test_sector_map_knows_the_universe_and_names_unknowns_by_themselves():
    assert sector_of("NVDA") == sector_of("XLK") == "technology"
    assert sector_label("JPM") == "פיננסים"
    assert sector_of("ZZZZ") == "ZZZZ"


def test_a_full_sector_refuses_another_name_from_it(tmp_path, clock):
    risk = manager(tmp_path, clock, max_sector_pct=40.0)
    tech = [held("NVDA", 40_000.0)]                     # 40% of 100k already tech
    decision = risk.evaluate(make_candidate(symbol="AMD"), make_account(), tech, make_stats())
    assert not decision.approved
    assert "הענף כבר מלא" in explain_reason(decision.reasons[-1])


def test_another_sector_is_still_open(tmp_path, clock):
    risk = manager(tmp_path, clock, max_sector_pct=40.0)
    tech = [held("NVDA", 40_000.0)]
    decision = risk.evaluate(make_candidate(symbol="JPM"), make_account(), tech, make_stats())
    assert decision.approved


def test_a_partly_used_sector_trims_the_size(tmp_path, clock):
    risk = manager(tmp_path, clock, max_sector_pct=40.0)
    decision = risk.evaluate(
        make_candidate(symbol="AMD"), make_account(), [held("NVDA", 39_000.0)], make_stats()
    )
    assert decision.approved
    assert decision.qty == 50          # $1,000 of room at $20 a share


def test_a_twin_of_a_held_name_is_refused(tmp_path, clock):
    risk = manager(tmp_path, clock, max_correlation=0.85)
    decision = risk.evaluate(
        make_candidate(symbol="AMD"), make_account(), [held("NVDA", 10_000.0)], make_stats(),
        twin=("NVDA", 0.93),
    )
    assert not decision.approved
    assert "אותו הימור פעמיים" in explain_reason(decision.reasons[-1])


def test_a_loosely_related_name_is_fine(tmp_path, clock):
    risk = manager(tmp_path, clock, max_correlation=0.85)
    decision = risk.evaluate(
        make_candidate(symbol="AMD"), make_account(), [held("NVDA", 10_000.0)], make_stats(),
        twin=("NVDA", 0.55),
    )
    assert decision.approved


def series(moves: list[float], skip: int | None = None) -> list[Bar]:
    price, bars = 100.0, []
    for i, move in enumerate(moves):
        price *= 1 + move
        if i != skip:
            bars.append(Bar(START + timedelta(days=i), price, price, price, price, 1))
    return bars


def test_correlation_matches_days_by_date_not_position():
    moves = [0.01 if i % 3 else -0.02 for i in range(80)]
    same = return_correlation(series(moves), series(moves, skip=40))
    opposite = return_correlation(series(moves), series([-m for m in moves]))
    assert same == pytest.approx(1.0, abs=0.05)
    assert opposite == pytest.approx(-1.0, abs=0.05)


def test_too_little_shared_history_is_unknown():
    assert return_correlation(series([0.01] * 10), series([0.01] * 10)) is None

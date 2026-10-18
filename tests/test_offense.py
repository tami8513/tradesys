from __future__ import annotations

from dataclasses import replace

import pytest

from conftest import make_account, make_candidate, make_profile, make_stats

from tradesys.agents.offense import OffenseAgent
from tradesys.agents.risk_manager import RiskManager
from tradesys.core.enums import OrderType, SetupType, Side, TimeInForce
from tradesys.core.models import OrderRequest
from tradesys.core.tenancy import TenantContext
from tradesys.persistence.journal import Journal


def setup_stack(tmp_path, clock):
    profile = make_profile()
    tenant = TenantContext.create(profile.client_id, tmp_path)
    journal = Journal(tenant)
    return profile, tenant, journal, OffenseAgent(tenant, profile, journal, clock)


def book(journal, clock, setup: SetupType, exit_price: float, n: int) -> None:
    """n closed trades in `setup`: entry 20, stop 18 (R = 2)."""
    candidate = make_candidate(setup=setup)
    for i in range(n):
        order = OrderRequest(
            client_id="tal_dahan", symbol="RIVN", side=Side.BUY, qty=10,
            order_type=OrderType.LIMIT, time_in_force=TimeInForce.DAY,
            client_order_id=f"{setup.value}-{i}", limit_price=20.0,
        )
        trade_id = journal.open_trade(candidate, order, 20.0, clock.now())
        journal.close_trade(trade_id, exit_price, clock.now(), "take_profit")


def test_new_setup_gets_three_quarters_of_the_ceiling(tmp_path, clock):
    _, _, _, offense = setup_stack(tmp_path, clock)
    [(_, conviction)] = offense.plan([make_candidate(setup=SetupType.BREAKOUT)])
    assert conviction == 0.75


def test_proven_setup_earns_full_size(tmp_path, clock):
    _, _, journal, offense = setup_stack(tmp_path, clock)
    book(journal, clock, SetupType.PULLBACK, 22.0, 8)          # +1R each
    [(_, conviction)] = offense.plan([make_candidate(setup=SetupType.PULLBACK)])
    assert conviction == 1.0


def test_losing_setup_is_cut_to_half(tmp_path, clock):
    _, _, journal, offense = setup_stack(tmp_path, clock)
    book(journal, clock, SetupType.BREAKOUT, 19.0, 8)          # -0.5R each
    [(_, conviction)] = offense.plan([make_candidate(setup=SetupType.BREAKOUT)])
    assert conviction == 0.5


def test_too_few_trades_never_count_as_proven(tmp_path, clock):
    _, _, journal, offense = setup_stack(tmp_path, clock)
    book(journal, clock, SetupType.PULLBACK, 26.0, 7)          # +3R, but only 7
    [(_, conviction)] = offense.plan([make_candidate(setup=SetupType.PULLBACK)])
    assert conviction == 0.75


def test_proven_setup_takes_the_slot_first(tmp_path, clock):
    _, _, journal, offense = setup_stack(tmp_path, clock)
    book(journal, clock, SetupType.PULLBACK, 22.0, 8)
    weaker = replace(make_candidate(symbol="KULR", setup=SetupType.PULLBACK), confidence=0.5)
    flashier = replace(make_candidate(symbol="RIVN", setup=SetupType.BREAKOUT), confidence=0.6)
    ranked = [c.symbol for c, _ in offense.plan([flashier, weaker])]
    assert ranked == ["KULR", "RIVN"]


def test_conviction_scales_size_and_can_never_exceed_the_ceiling(tmp_path, clock):
    profile, tenant, journal, _ = setup_stack(tmp_path, clock)
    risk = RiskManager(tenant, profile, journal, clock)

    def qty(conviction: float) -> int:
        decision = risk.evaluate(
            make_candidate(), make_account(), [], make_stats(), conviction=conviction
        )
        return decision.order.qty

    full = qty(1.0)
    assert qty(0.5) == pytest.approx(full / 2, abs=1)
    assert qty(5.0) == full

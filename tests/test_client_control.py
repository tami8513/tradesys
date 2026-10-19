from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from conftest import make_account, make_candidate, make_profile, make_stats

from tradesys.agents.risk_manager import RiskManager
from tradesys.brokers.paper import PaperBroker
from tradesys.clients.profile import RISK_LEVELS
from tradesys.clients.tracks import Control, apply_control, read_control, write_control
from tradesys.core.models import Position
from tradesys.core.tenancy import TenantContext
from tradesys.data.market_data import SyntheticMarketData
from tradesys.data.regime import MarketRegime
from tradesys.persistence.journal import Journal

NOW = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)
RISING = MarketRegime("SPY", 520.0, 500.0, 200, NOW)
FALLING = MarketRegime("SPY", 480.0, 500.0, 200, NOW)


def manager(tmp_path, clock, **risk):
    base = make_profile()
    profile = replace(base, risk=replace(base.risk, **risk))
    tenant = TenantContext.create(profile.client_id, tmp_path)
    return RiskManager(tenant, profile, Journal(tenant), clock)


def qty(risk, positions=(), regime=None) -> int:
    decision = risk.evaluate(make_candidate(), make_account(), list(positions), make_stats(), regime)
    return decision.order.qty if decision.approved else 0


def test_allocation_sizes_on_the_allocated_money(tmp_path, clock):
    whole = qty(manager(tmp_path / "a", clock))
    allocated = qty(manager(tmp_path / "b", clock, allocation_cap=20_000.0))
    assert allocated == pytest.approx(whole / 5, abs=1)


def test_allocation_caps_total_exposure(tmp_path, clock):
    held = Position("tal_dahan", "KULR", 950, 20.0, 20.0)   # $19,000 already invested
    assert qty(manager(tmp_path, clock, allocation_cap=20_000.0), [held]) == 50


def test_autopilot_is_bolder_only_in_a_confirmed_rising_market(tmp_path, clock):
    risk = manager(tmp_path, clock, auto_pilot=True, max_daily_loss_pct=1.5)
    rising, falling, unknown = qty(risk, regime=RISING), qty(risk, regime=FALLING), qty(risk)
    assert rising == pytest.approx(3 * falling, abs=1)    # 0.75% vs 0.25%
    assert unknown == falling


def test_autopilot_uses_the_balanced_limits(tmp_path):
    profile = apply_control(make_profile(), Control("auto", None, 100_000.0), None)
    level = RISK_LEVELS[2]
    assert profile.risk.auto_pilot
    assert profile.risk.max_drawdown_pct == level.max_drawdown_pct
    assert profile.risk.capital_floor_pct == level.capital_floor_pct


def test_custom_mode_keeps_the_clients_level_and_adds_the_allocation(tmp_path):
    from tradesys.clients.tracks import TrackChoice

    profile = apply_control(
        make_profile(), Control("custom", 30_000.0, 100_000.0), TrackChoice(1, 100_000.0, "")
    )
    assert not profile.risk.auto_pilot
    assert profile.risk.max_drawdown_pct == RISK_LEVELS[1].max_drawdown_pct
    assert profile.risk.allocation_cap == 30_000.0


def test_control_round_trips_through_the_tenant_dir(tmp_path):
    write_control(tmp_path, "custom", 25_000.0, 100_000.0)
    assert read_control(tmp_path) == Control("custom", 25_000.0, 100_000.0)


def test_a_deposit_is_not_profit(tmp_path, clock):
    tenant = TenantContext.create("tal_dahan", tmp_path)
    journal = Journal(tenant)
    broker = PaperBroker(tenant, SyntheticMarketData())
    journal.roll_session(date(2026, 10, 19), 100_000.0)

    broker.deposit(25_000.0)
    journal.record_deposit(25_000.0)

    assert broker.account().equity == pytest.approx(125_000.0)
    assert journal.capital_base() == pytest.approx(125_000.0)
    assert journal.day_start_equity() == pytest.approx(125_000.0)
    assert journal.day_stats(date(2026, 10, 19)).peak_equity == pytest.approx(125_000.0)


def test_autopilot_reads_the_market_even_without_the_regime_filter(tmp_path):
    from pathlib import Path

    from tradesys.core.config import Settings
    from tradesys.orchestration.engine import TradingSystem

    root = Path(__file__).resolve().parent.parent
    system = TradingSystem(
        Settings(clients_dir=root / "clients", data_dir=tmp_path),
        market_data=SyntheticMarketData(),
    )
    tenant_dir = system.tenant("demo_showcase").data_dir
    write_control(tenant_dir, "auto", None, 100_000.0)
    session = system.for_client("demo_showcase")
    try:
        assert not session.profile.risk.regime_blocked_setups
        assert session.market_regime() is not None
    finally:
        session.close()

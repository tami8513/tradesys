from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_account, make_candidate, make_profile, make_stats

from tradesys.agents.risk_manager import RiskManager
from tradesys.core.enums import SetupType, Side
from tradesys.core.tenancy import TenantContext
from tradesys.persistence.journal import Journal


def build(tmp_path: Path, clock, **profile_kwargs) -> tuple[RiskManager, Journal]:
    profile = make_profile(**profile_kwargs)
    tenant = TenantContext.create(profile.client_id, tmp_path)
    journal = Journal(tenant)
    return RiskManager(tenant, profile, journal, clock), journal


def test_rejects_symbol_outside_watchlist(tmp_path, clock):
    risk, _ = build(tmp_path, clock)
    decision = risk.evaluate(
        make_candidate(symbol="TSLA"), make_account(), [], make_stats()
    )
    assert not decision.approved
    assert decision.order is None
    assert any("watchlist" in reason for reason in decision.reasons)


def test_rejects_setup_not_enabled_for_symbol(tmp_path, clock):
    risk, _ = build(tmp_path, clock)
    decision = risk.evaluate(
        make_candidate(setup=SetupType.OVERSOLD_REVERSAL),
        make_account(),
        [],
        make_stats(),
    )
    assert not decision.approved


def test_rejects_short_when_disabled(tmp_path, clock):
    risk, _ = build(tmp_path, clock)
    decision = risk.evaluate(
        make_candidate(side=Side.SELL, stop=22.0, target=14.0),
        make_account(),
        [],
        make_stats(),
    )
    assert not decision.approved
    assert any("short selling" in reason for reason in decision.reasons)


def test_rejects_reward_risk_below_minimum(tmp_path, clock):
    risk, _ = build(tmp_path, clock)
    decision = risk.evaluate(
        make_candidate(entry=20.0, stop=18.0, target=22.0),  # 1:1
        make_account(),
        [],
        make_stats(),
    )
    assert not decision.approved
    assert any("reward:risk" in reason for reason in decision.reasons)


def test_rejects_when_daily_trade_cap_reached(tmp_path, clock):
    risk, _ = build(tmp_path, clock, max_trades_per_day=3)
    decision = risk.evaluate(
        make_candidate(), make_account(), [], make_stats(trades_opened=3)
    )
    assert not decision.approved


def test_position_size_follows_risk_per_trade(tmp_path, clock):
    # 0.5% of 100k = 500 risk budget; 2.00 per share of stop distance -> 250 shares.
    risk, _ = build(tmp_path, clock)
    decision = risk.evaluate(make_candidate(), make_account(), [], make_stats())
    assert decision.approved
    assert decision.qty == 250
    assert decision.risk_amount == pytest.approx(500.0)


def test_position_cap_overrides_risk_sizing(tmp_path, clock):
    # 250 shares x $20 = $5,000 notional, but a 2% cap on 100k allows only $2,000.
    risk, _ = build(tmp_path, clock, max_position_pct=2.0)
    decision = risk.evaluate(make_candidate(), make_account(), [], make_stats())
    assert decision.approved
    assert decision.qty == 100


def test_liquidity_cap_applies(tmp_path, clock):
    risk, _ = build(tmp_path, clock)
    decision = risk.evaluate(
        make_candidate(avg_volume=1_000.0),  # 2% of ADV = 20 shares
        make_account(),
        [],
        make_stats(),
    )
    assert decision.approved
    assert decision.qty == 20


def test_two_tenants_size_the_same_idea_differently(tmp_path, clock):
    conservative, _ = build(tmp_path / "a", clock, risk_per_trade_pct=0.5)
    aggressive, _ = build(
        tmp_path / "b", clock, client_id="client_001", risk_per_trade_pct=1.0
    )
    lenient = aggressive.evaluate(
        make_candidate(client_id="client_001"),
        make_account("client_001"),
        [],
        make_stats("client_001"),
    )
    strict = conservative.evaluate(make_candidate(), make_account(), [], make_stats())
    assert strict.qty * 2 == lenient.qty


def test_drawdown_breach_halts_and_latches(tmp_path, clock):
    risk, journal = build(tmp_path, clock, max_drawdown_pct=2.0)
    assert risk.preflight(make_account(equity=100_000.0)).ok

    breached = risk.preflight(make_account(equity=97_000.0))
    assert not breached.ok
    assert "drawdown" in breached.reason
    assert journal.halt_state()[0]

    # Equity recovering does not un-halt the tenant; only an operator can.
    assert not risk.preflight(make_account(equity=101_000.0)).ok
    journal.clear_halt()
    assert risk.preflight(make_account(equity=101_000.0)).ok


def test_order_id_is_deterministic_per_day(tmp_path, clock):
    risk, _ = build(tmp_path, clock)
    first = risk.evaluate(make_candidate(), make_account(), [], make_stats())
    second = risk.evaluate(make_candidate(), make_account(), [], make_stats())
    assert first.order.client_order_id == second.order.client_order_id


def test_releasing_a_drawdown_halt_rebases_the_peak(tmp_path, clock):
    risk, journal = build(tmp_path, clock, max_drawdown_pct=2.0)
    assert risk.preflight(make_account(equity=100_000.0)).ok
    assert not risk.preflight(make_account(equity=97_000.0)).ok

    # Without a rebase the same drawdown halts again on the next check.
    journal.clear_halt()
    assert not risk.preflight(make_account(equity=97_000.0)).ok

    journal.clear_halt(rebase_peak_to=97_000.0)
    assert risk.preflight(make_account(equity=97_000.0)).ok


def test_without_leverage_an_entry_is_capped_by_cash_not_margin_buying_power(tenant, journal, clock):
    from dataclasses import replace

    from conftest import make_account, make_candidate, make_profile, make_stats
    from tradesys.agents.risk_manager import RiskManager

    margin_account = replace(make_account(), cash=1_000.0, buying_power=400_000.0)
    decision = RiskManager(tenant, make_profile(), journal, clock).evaluate(
        make_candidate(), margin_account, [], make_stats()
    )

    assert decision.qty * make_candidate().entry <= 1_000.0

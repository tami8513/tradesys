from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import make_account, make_candidate, make_profile, make_stats

from tradesys.agents.risk_manager import RiskManager
from tradesys.clients.picks import (
    MAX_PICKS,
    PICKS_FILE,
    Pick,
    apply_picks,
    normalize,
    read_picks,
    write_picks,
)
from tradesys.clients.proposals import pending, propose
from tradesys.core.errors import ConfigError
from tradesys.core.tenancy import TenantContext
from tradesys.persistence.journal import Journal

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("typed", ["nvda", " NVDA ", "BRK.B"])
def test_tickers_are_normalized(typed):
    assert normalize(typed) == typed.strip().upper()


@pytest.mark.parametrize("typed", ["", "TOOLONG", "NV DA", "1234", "../x"])
def test_bad_tickers_are_refused(typed):
    with pytest.raises(ConfigError):
        normalize(typed)


def test_picks_round_trip_with_their_options(tmp_path):
    write_picks(tmp_path, [Pick("nvda", priority=True), Pick("AAPL", ask_first=True), Pick("NVDA")])
    assert read_picks(tmp_path) == [Pick("NVDA", priority=True), Pick("AAPL", ask_first=True)]


def test_picks_saved_before_the_options_still_load(tmp_path):
    (tmp_path / PICKS_FILE).write_text(json.dumps({"symbols": ["NVDA", "AAPL"]}), encoding="utf-8")
    assert read_picks(tmp_path) == [Pick("NVDA"), Pick("AAPL")]


def test_at_most_ten_picks(tmp_path):
    with pytest.raises(ConfigError):
        write_picks(tmp_path, [Pick(f"A{chr(65 + i)}") for i in range(MAX_PICKS + 1)])


def test_a_pick_joins_the_watchlist_with_its_flags():
    profile = apply_picks(
        make_profile(symbols=("KULR",)),
        [Pick("NVDA", priority=True), Pick("AAPL", ask_first=True)],
    )
    assert {"NVDA", "AAPL"} <= set(profile.symbols)
    assert profile.pinned_symbols == ("NVDA", "AAPL")
    assert profile.priority_symbols == ("NVDA",)
    assert profile.approval_symbols == ("AAPL",)


def test_a_pick_still_goes_through_every_risk_check(tmp_path, clock):
    profile = apply_picks(make_profile(symbols=("KULR",)), [Pick("NVDA")])
    tenant = TenantContext.create(profile.client_id, tmp_path)
    risk = RiskManager(tenant, profile, Journal(tenant), clock)

    assert risk.evaluate(make_candidate(symbol="NVDA"), make_account(), [], make_stats()).approved
    too_cheap = risk.evaluate(
        make_candidate(symbol="NVDA", entry=0.5, stop=0.4, target=0.8),
        make_account(), [], make_stats(),
    )
    assert not too_cheap.approved


def test_one_waiting_proposal_per_stock_until_it_expires(tmp_path):
    close = NOW + timedelta(hours=2)
    assert propose(tmp_path, make_candidate(symbol="NVDA"), 20, NOW, close) is not None
    assert propose(tmp_path, make_candidate(symbol="NVDA"), 20, NOW, close) is None
    assert [p.symbol for p in pending(tmp_path, NOW)] == ["NVDA"]
    assert pending(tmp_path, close) == []


# ------------------------------------------------------------- full cycle

MARKET_HOURS = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)     # 11:00 New York


def demo_session(tmp_path, picks):
    from dataclasses import replace

    from tradesys.core.clock import FrozenClock
    from tradesys.core.config import Settings
    from tradesys.data.market_data import SyntheticMarketData
    from tradesys.orchestration.engine import TradingSystem

    root = Path(__file__).resolve().parent.parent
    market = SyntheticMarketData()
    system = TradingSystem(
        Settings(clients_dir=root / "clients", data_dir=tmp_path),
        market_data=market,
        clock=FrozenClock(MARKET_HOURS),
    )
    write_picks(system.tenant("demo_showcase").data_dir, picks)
    session = system.for_client("demo_showcase")
    live = market.last_price("RIVN")
    idea = replace(
        make_candidate(client_id="demo_showcase", symbol="RIVN"),
        entry=live, stop=live * 0.95, target=live * 1.25,
    )
    session.scout.scan = lambda focus=None: [idea]
    return session


def test_an_ask_first_pick_is_proposed_not_bought(tmp_path):
    session = demo_session(tmp_path, [Pick("RIVN", ask_first=True)])
    try:
        cycle = session.run_cycle()
        waiting = pending(session.tenant.data_dir, MARKET_HOURS)
        assert cycle.approvals == 1 and cycle.submissions == 0
        assert [p.symbol for p in waiting] == ["RIVN"]
        assert session.broker.positions() == []
    finally:
        session.close()


def test_approving_re_checks_and_buys(tmp_path):
    session = demo_session(tmp_path, [Pick("RIVN", ask_first=True)])
    try:
        session.run_cycle()
        [proposal] = pending(session.tenant.data_dir, MARKET_HOURS)
        note = session.execute_proposal(proposal.proposal_id)
        assert note.startswith("נקנו")
        assert [p.symbol for p in session.broker.positions()] == ["RIVN"]
        assert pending(session.tenant.data_dir, MARKET_HOURS) == []
        assert session.execute_proposal(proposal.proposal_id) == "הבקשה כבר טופלה."
    finally:
        session.close()


def test_declining_buys_nothing(tmp_path):
    session = demo_session(tmp_path, [Pick("RIVN", ask_first=True)])
    try:
        session.run_cycle()
        [proposal] = pending(session.tenant.data_dir, MARKET_HOURS)
        session.decline_proposal(proposal.proposal_id)
        assert pending(session.tenant.data_dir, MARKET_HOURS) == []
        assert session.broker.positions() == []
    finally:
        session.close()


def test_a_plain_pick_is_bought_without_asking(tmp_path):
    session = demo_session(tmp_path, [Pick("RIVN")])
    try:
        assert session.run_cycle().submissions == 1
        assert pending(session.tenant.data_dir, MARKET_HOURS) == []
    finally:
        session.close()


def test_a_priority_pick_is_evaluated_before_the_robots_own_ideas(tmp_path):
    from dataclasses import replace

    session = demo_session(tmp_path, [Pick("KULR", priority=True)])
    try:
        live = session.market_data.last_price("KULR")
        own = session.scout.scan()[0]                       # RIVN, the robot's idea
        mine = replace(
            make_candidate(client_id="demo_showcase", symbol="KULR"),
            entry=live, stop=live * 0.95, target=live * 1.25, confidence=0.1,
        )
        session.scout.scan = lambda focus=None: [own, mine]
        session.run_cycle()
        order = [e["symbol"] for e in session.journal.agent_events(MARKET_HOURS - timedelta(1))
                 if e["agent"] == "risk"]
        assert order[0] == "KULR"
    finally:
        session.close()


def test_approval_never_buys_more_than_the_client_agreed_to(tmp_path):
    """Found on screen: a request for 40 shares bought 916 after the re-check."""
    from tradesys.clients.proposals import propose

    session = demo_session(tmp_path, [Pick("RIVN", ask_first=True)])
    try:
        idea = session.scout.scan()[0]
        proposal = propose(session.tenant.data_dir, idea, 7, MARKET_HOURS,
                           MARKET_HOURS + timedelta(hours=1))
        assert session.execute_proposal(proposal.proposal_id).startswith("נקנו 7 ")
        [held] = session.broker.positions()
        assert held.qty == 7
    finally:
        session.close()


def test_an_approval_given_after_the_close_is_for_the_next_trading_day():
    from tradesys.clients.picks import approval_day

    friday_night = datetime(2026, 10, 23, 22, 0, tzinfo=timezone.utc)      # 18:00 New York
    tuesday_morning = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)   # 08:00 New York
    assert approval_day(friday_night).isoformat() == "2026-10-26"         # Monday
    assert approval_day(tuesday_morning).isoformat() == "2026-10-20"


def test_a_preapproved_pick_is_bought_without_asking(tmp_path):
    today = MARKET_HOURS.date().isoformat()
    session = demo_session(tmp_path, [Pick("RIVN", ask_first=True, preapproved_on=today)])
    try:
        assert session.run_cycle().submissions == 1
        assert pending(session.tenant.data_dir, MARKET_HOURS) == []
    finally:
        session.close()


def test_the_standing_approval_caps_the_amount(tmp_path):
    today = MARKET_HOURS.date().isoformat()
    session = demo_session(
        tmp_path, [Pick("RIVN", ask_first=True, preapproved_on=today, max_amount=100.0)]
    )
    try:
        session.run_cycle()
        [held] = session.broker.positions()
        assert held.qty * held.avg_entry_price <= 100.0
    finally:
        session.close()


def test_yesterdays_approval_does_not_count_today(tmp_path):
    yesterday = (MARKET_HOURS.date() - timedelta(days=1)).isoformat()
    session = demo_session(tmp_path, [Pick("RIVN", ask_first=True, preapproved_on=yesterday)])
    try:
        assert session.run_cycle().submissions == 0
        assert [p.symbol for p in pending(session.tenant.data_dir, MARKET_HOURS)] == ["RIVN"]
    finally:
        session.close()


def test_a_budget_below_one_share_buys_nothing(tmp_path):
    today = MARKET_HOURS.date().isoformat()
    session = demo_session(
        tmp_path, [Pick("RIVN", ask_first=True, preapproved_on=today, max_amount=1.0)]
    )
    try:
        assert session.run_cycle().submissions == 0
        assert session.broker.positions() == []
    finally:
        session.close()


def test_an_approval_is_capped_even_without_an_amount_or_above_the_limit(tmp_path):
    from tradesys.clients.picks import PREAPPROVAL_MAX

    today = MARKET_HOURS.date().isoformat()
    session = demo_session(tmp_path, [
        Pick("RIVN", ask_first=True, preapproved_on=today),
        Pick("KULR", ask_first=True, preapproved_on=today, max_amount=1_000_000.0),
    ])
    try:
        assert session._standing_approval("RIVN") == PREAPPROVAL_MAX
        assert session._standing_approval("KULR") == PREAPPROVAL_MAX
    finally:
        session.close()


def test_a_lab_plan_narrows_the_setups_and_sets_the_stop_for_that_pick_only(tmp_path):
    from tradesys.core.enums import SetupType

    write_picks(tmp_path, [Pick("NVDA", priority=True, setups=("pullback",), stop_atr=4.0), Pick("KO")])
    picks = read_picks(tmp_path)
    profile = apply_picks(make_profile(), picks)

    assert picks[0].setups == ("pullback",) and picks[0].stop_atr == 4.0
    assert profile.entry_for("NVDA").setups == frozenset({SetupType.PULLBACK})
    assert len(profile.entry_for("KO").setups) == len(SetupType)
    assert profile.stop_multiple("NVDA") == 4.0
    assert profile.stop_multiple("KO") == profile.stop_atr_multiple

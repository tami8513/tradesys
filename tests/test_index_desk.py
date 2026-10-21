from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pytest import approx

from conftest import make_profile

from tradesys.brokers.paper import PaperBroker
from tradesys.clients.index_sleeve import clean_settings, read_ledger, write_settings
from tradesys.core.clock import FrozenClock
from tradesys.core.tenancy import TenantContext
from tradesys.data.market_data import SyntheticMarketData
from tradesys.orchestration.index_desk import book_account, reserved_symbols, run_index_robot
from tradesys.orchestration.watchdog import inspect
from tradesys.persistence.journal import Journal

NEW_YORK = ZoneInfo("America/New_York")
MORNING = datetime(2026, 10, 21, 10, 0, tzinfo=NEW_YORK)
BEFORE_CLOSE = datetime(2026, 10, 21, 15, 10, tzinfo=NEW_YORK)


def session(tmp_path, *, enabled=True, now=BEFORE_CLOSE):
    tenant = TenantContext.create("tal_dahan", tmp_path)
    market = SyntheticMarketData()
    write_settings(tenant.data_dir, clean_settings(enabled, 20_000, ("SPY", "QQQ"), False, True))
    return SimpleNamespace(
        tenant=tenant,
        client_id="tal_dahan",
        journal=Journal(tenant),
        broker=PaperBroker(tenant, market),
        profile=make_profile(),
        market_data=market,
        clock=FrozenClock(now),
    )


def test_it_decides_once_a_day_an_hour_before_the_close(tmp_path):
    early = session(tmp_path, now=MORNING)
    assert run_index_robot(early, can_buy=True) == []

    s = session(tmp_path)
    events = run_index_robot(s, can_buy=True)

    assert {e[2] for e in events if e[:2] == ("index", "buy")} == {"QQQ", "SPY"}
    held = {p.symbol: p.qty for p in s.broker.positions()}
    assert {k: h.qty for k, h in read_ledger(s.tenant.data_dir).holdings.items()} == held
    assert run_index_robot(s, can_buy=True) == []


def test_off_or_halted_it_does_not_buy(tmp_path):
    assert run_index_robot(session(tmp_path / "off", enabled=False), can_buy=True) == []
    halted = session(tmp_path / "halted")
    assert not [e for e in run_index_robot(halted, can_buy=False) if e[1] == "buy"]


def test_its_funds_belong_to_it_and_not_to_the_main_robot(tmp_path):
    s = session(tmp_path)
    before = s.broker.account().equity
    run_index_robot(s, can_buy=True)

    assert book_account(s).equity == approx(before)
    assert reserved_symbols(s.tenant.data_dir) == {"SPY", "QQQ"}
    assert not [a for a in inspect(s) if a.key.startswith("orphan:")]


def test_a_full_cycle_runs_the_index_robot_and_keeps_the_main_robot_off_its_indices(tmp_path):
    from dataclasses import replace
    from pathlib import Path

    from conftest import make_candidate

    from tradesys.core.config import Settings
    from tradesys.orchestration.engine import TradingSystem

    root = Path(__file__).resolve().parent.parent
    market = SyntheticMarketData()
    system = TradingSystem(
        Settings(clients_dir=root / "clients", data_dir=tmp_path),
        market_data=market,
        clock=FrozenClock(BEFORE_CLOSE),
    )
    data_dir = system.tenant("demo_showcase").data_dir
    write_settings(data_dir, clean_settings(True, 10_000, ("SPY",), False, True))
    s = system.for_client("demo_showcase")
    live = market.last_price("SPY")
    idea = replace(
        make_candidate(client_id="demo_showcase", symbol="SPY"),
        entry=live, stop=live * 0.95, target=live * 1.25,
    )
    s.scout.scan = lambda focus=None: [idea]
    try:
        cycle = s.run_cycle()
        assert cycle.candidates == 0
        assert s.journal.open_trades() == []
        assert read_ledger(data_dir).qty("SPY") > 0
    finally:
        s.close()

"""Scheduler, market clock and the shared automation switch."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime
from pathlib import Path

from tradesys.clients.automation import automation_enabled, lifecycle_active, set_automation
from tradesys.core.config import Settings
from tradesys.data.market_clock import NEW_YORK, AlpacaMarketClock, RegularHoursClock
from tradesys.data.market_data import SyntheticMarketData
from tradesys.orchestration.engine import TradingSystem
from tradesys.scheduler import due_tenants, run_once

ROOT = Path(__file__).resolve().parent.parent


def ny(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NEW_YORK)


# ------------------------------------------------------------------- clock

def test_regular_hours_open_midday_on_a_weekday():
    assert RegularHoursClock(lambda: ny(2026, 10, 19, 11, 0)).status().is_open


def test_regular_hours_closed_before_the_open_and_opens_same_day():
    status = RegularHoursClock(lambda: ny(2026, 10, 19, 8, 0)).status()
    assert not status.is_open
    assert status.next_open == ny(2026, 10, 19, 9, 30)


def test_regular_hours_friday_evening_next_open_is_monday():
    status = RegularHoursClock(lambda: ny(2026, 10, 16, 17, 0)).status()
    assert not status.is_open
    assert status.next_open == ny(2026, 10, 19, 9, 30)


def test_alpaca_clock_is_used_when_reachable(monkeypatch):
    module = types.ModuleType("httpx")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"is_open": True, "next_open": "2026-10-20T09:30:00-04:00"}

    module.get = lambda url, headers=None, timeout=None: Response()
    monkeypatch.setitem(sys.modules, "httpx", module)

    status = AlpacaMarketClock("k", "s").status()
    assert status.is_open and status.source == "alpaca"


def test_alpaca_clock_falls_back_when_unreachable(monkeypatch):
    module = types.ModuleType("httpx")

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    module.get = boom
    monkeypatch.setitem(sys.modules, "httpx", module)

    fallback = RegularHoursClock(lambda: ny(2026, 10, 19, 11, 0))
    status = AlpacaMarketClock("k", "s", fallback=fallback).status()
    assert status.source == "regular-hours" and status.is_open


# --------------------------------------------------------------- automation

def test_default_autorun_list_applies_until_the_client_chooses(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADESYS_AUTORUN", "demo_showcase, tal_dahan")
    assert automation_enabled(tmp_path, "tal_dahan")
    assert not automation_enabled(tmp_path, "client_001")

    set_automation(tmp_path, False)
    assert not automation_enabled(tmp_path, "tal_dahan")


def test_suspended_tenant_is_not_active(tmp_path):
    assert lifecycle_active(tmp_path)
    (tmp_path / "lifecycle.json").write_text(json.dumps({"active": False}), encoding="utf-8")
    assert not lifecycle_active(tmp_path)


# ---------------------------------------------------------------- scheduler

def system(tmp_path: Path) -> TradingSystem:
    return TradingSystem(
        Settings(clients_dir=ROOT / "clients", data_dir=tmp_path),
        market_data=SyntheticMarketData(),
    )


def test_only_enabled_and_active_tenants_are_due(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADESYS_AUTORUN", "demo_showcase,tal_dahan")
    trading = system(tmp_path)
    (trading.tenant("tal_dahan").data_dir / "lifecycle.json").write_text(
        json.dumps({"active": False}), encoding="utf-8"
    )
    assert due_tenants(trading) == ["demo_showcase"]


def test_run_once_runs_a_cycle_per_due_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADESYS_AUTORUN", "demo_showcase,client_001")
    outcomes = run_once(system(tmp_path))
    assert set(outcomes) == {"client_001", "demo_showcase"}
    assert all(not text.startswith("failed") for text in outcomes.values())


def test_one_broken_tenant_does_not_stop_the_others(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADESYS_AUTORUN", "demo_showcase,client_001")
    trading = system(tmp_path)
    original = trading.for_client

    def flaky(client_id):
        if client_id == "client_001":
            raise RuntimeError("broker down")
        return original(client_id)

    trading.for_client = flaky
    outcomes = run_once(trading)
    assert outcomes["client_001"].startswith("failed")
    assert not outcomes["demo_showcase"].startswith("failed")


def test_request_urls_are_not_logged():
    """Finnhub's key travels in the URL; httpx must not log URLs at INFO."""
    import logging

    from tradesys.core import logging as tradesys_logging

    tradesys_logging._CONFIGURED = False
    tradesys_logging.configure("INFO")
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_watch_sends_new_findings_and_a_crashed_cycle(tmp_path):
    from tradesys.core.enums import OrderType, Side, TimeInForce
    from tradesys.core.models import OrderRequest
    from tradesys.orchestration.watchdog import AlertBook
    from tradesys.scheduler import watch

    system = TradingSystem(
        Settings(clients_dir=ROOT / "clients", data_dir=tmp_path),
        market_data=SyntheticMarketData(),
    )
    for client_id in system.registry.list_ids():
        set_automation(system.tenant(client_id).data_dir, client_id == "demo_showcase")

    session = system.for_client("demo_showcase")
    session.broker.submit(OrderRequest(
        client_id="demo_showcase", symbol="RIVN", side=Side.BUY, qty=5,
        order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY, client_order_id="orphan",
    ))
    session.close()

    class Recorder:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    notifier = Recorder()
    book = AlertBook(tmp_path / "alerts.json")
    watch(system, book, notifier, {"demo_showcase": "failed: RuntimeError: boom"})

    assert any("RIVN" in m and "לא רשומה במערכת" in m for m in notifier.sent)
    assert any("סבב המסחר נכשל" in m for m in notifier.sent)
    watch(system, book, notifier)          # same problems, minutes later: no repeat
    assert sum("RIVN" in m for m in notifier.sent) == 1

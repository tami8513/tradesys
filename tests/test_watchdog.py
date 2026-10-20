from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from conftest import make_candidate, make_profile

from tradesys.brokers.paper import PaperBroker
from tradesys.core.enums import OrderType, Side, TimeInForce
from tradesys.core.models import OrderRequest
from tradesys.core.tenancy import TenantContext
from tradesys.data.market_data import SyntheticMarketData
from tradesys.orchestration.watchdog import Alert, AlertBook, cycle_failure, inspect
from tradesys.persistence.journal import Journal

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def order(symbol: str, qty: int = 10) -> OrderRequest:
    return OrderRequest(
        client_id="tal_dahan", symbol=symbol, side=Side.BUY, qty=qty,
        order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
        client_order_id=f"wd-{symbol}",
    )


def session(tmp_path, broker_cls=PaperBroker):
    tenant = TenantContext.create("tal_dahan", tmp_path)
    market = SyntheticMarketData()
    return SimpleNamespace(
        journal=Journal(tenant),
        broker=broker_cls(tenant, market),
        profile=make_profile(),
        market_data=market,
    )


def keys(alerts) -> set[str]:
    return {a.key for a in alerts}


def test_a_healthy_book_raises_nothing(tmp_path):
    s = session(tmp_path)
    s.broker.submit(order("RIVN"))
    s.journal.open_trade(make_candidate(), order("RIVN"), 20.0, NOW)
    assert not [a for a in inspect(s) if a.level != "info"]


def test_a_position_nobody_journaled_is_critical(tmp_path):
    s = session(tmp_path)
    s.broker.submit(order("KULR"))
    [orphan] = [a for a in inspect(s) if a.key == "orphan:KULR"]
    assert orphan.level == "critical"


def test_a_journaled_trade_missing_at_the_broker_is_flagged(tmp_path):
    s = session(tmp_path)
    s.journal.open_trade(make_candidate(), order("RIVN"), 20.0, NOW)
    assert "missing:RIVN" in keys(inspect(s))


def test_a_position_without_a_resting_stop_is_critical(tmp_path):
    class NoStops(PaperBroker):
        def resting_stop(self, symbol):
            return None

    s = session(tmp_path, NoStops)
    s.broker.submit(order("RIVN"))
    s.journal.open_trade(make_candidate(), order("RIVN"), 20.0, NOW)
    assert "unprotected:RIVN" in keys(inspect(s))


def test_a_halt_and_a_deep_drawdown_are_reported(tmp_path):
    s = session(tmp_path)
    s.journal.roll_session(date.today(), 102_000.0)       # peak above today's 100k
    assert "drawdown" in keys(inspect(s))                  # 1.96% of a 2% limit
    s.journal.halt("drawdown 2.10% >= limit 2.00%")
    found = keys(inspect(s))
    assert "halted" in found and "drawdown" not in found


def test_a_failed_cycle_becomes_an_alert():
    assert cycle_failure("tal_dahan", "failed: BrokerError: timeout").level == "critical"
    assert cycle_failure("tal_dahan", "[tal_dahan] 0 exit(s)") is None


def test_an_alert_is_sent_once_repeated_later_and_closed_when_resolved(tmp_path):
    book = AlertBook(tmp_path / "alerts.json")
    alert = Alert("unprotected:RIVN", "critical", "ל-RIVN אין פקודת הגנה")

    first = book.update("tal_dahan", [alert], NOW)
    assert len(first) == 1 and first[0].startswith("🔴")
    assert book.update("tal_dahan", [alert], NOW + timedelta(hours=1)) == []
    [again] = book.update("tal_dahan", [alert], NOW + timedelta(hours=7))
    assert "עדיין" in again
    [resolved] = book.update("tal_dahan", [], NOW + timedelta(hours=8))
    assert resolved.startswith("✅") and "נפתר" in resolved
    assert book.active()["tal_dahan"] == {}

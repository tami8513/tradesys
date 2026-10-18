"""Two processes sharing one tenant's files — the server's dashboard + scheduler.

Each store instance below keeps its own in-memory copy, exactly as two separate
processes would. Before the fix, every write replaced the whole file with the
writer's stale copy, so the other process's fills and lessons were lost.
"""

from __future__ import annotations

from tradesys.brokers.paper import PaperBroker
from tradesys.core.enums import MemoryKind, OrderType, Side, TimeInForce
from tradesys.core.models import MemoryRecord, OrderRequest
from tradesys.core.tenancy import TenantContext
from tradesys.data.market_data import SyntheticMarketData
from tradesys.memory.store import LocalVectorMemory


def order(symbol: str, qty: int, price: float, tag: str) -> OrderRequest:
    return OrderRequest(
        client_id="tal_dahan",
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        client_order_id=tag,
        limit_price=price,
    )


def test_two_ledgers_do_not_erase_each_others_fills(tmp_path):
    tenant = TenantContext.create("tal_dahan", tmp_path)
    market = SyntheticMarketData()
    dashboard = PaperBroker(tenant, market)
    scheduler = PaperBroker(tenant, market)

    scheduler.submit(order("RIVN", 100, 10.0, "a"))
    dashboard.submit(order("KULR", 50, 2.0, "b"))
    scheduler.submit(order("SQQQ", 10, 40.0, "c"))

    held = {p.symbol: p.qty for p in dashboard.positions()}
    assert held == {"RIVN": 100, "KULR": 50, "SQQQ": 10}
    assert dashboard.account().cash == 100_000 - 1_000 - 100 - 400
    assert {p.symbol for p in scheduler.positions()} == set(held)


def test_two_memories_do_not_erase_each_others_lessons(tmp_path):
    tenant = TenantContext.create("tal_dahan", tmp_path)
    dashboard = LocalVectorMemory(tenant)
    scheduler = LocalVectorMemory(tenant)

    def lesson(text: str) -> MemoryRecord:
        return MemoryRecord(client_id="tal_dahan", kind=MemoryKind.TRADE_POSTMORTEM, text=text)

    scheduler.add([lesson("RIVN stopped out")])
    dashboard.add([lesson("operator: avoid breakouts")])
    scheduler.add([lesson("SQQQ hit target")])

    assert dashboard.count() == 3
    assert {r.text for r in dashboard.recent(10)} == {
        "RIVN stopped out",
        "operator: avoid breakouts",
        "SQQQ hit target",
    }

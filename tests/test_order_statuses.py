from __future__ import annotations


def test_recent_orders_are_the_newest_ones(journal):
    for day, status in [(1, "canceled"), (2, "canceled"), (3, "filled"), (4, "filled")]:
        journal._execute(
            "INSERT INTO orders (client_order_id, client_id, broker_order_id, symbol, side, qty,"
            " status, submitted_at, payload) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"o{day}", "tal_dahan", f"b{day}", "AAPL", "buy", 1, status, f"2026-10-0{day}T14:00:00", "{}"),
        )

    assert journal.order_statuses(recent=2) == ["filled", "filled"]
    assert sorted(journal.order_statuses()) == ["canceled", "canceled", "filled", "filled"]

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from tradesys.core.tenancy import TenantContext
from tradesys.persistence.journal import Journal


def test_first_session_day_is_recorded_once(tmp_path):
    journal = Journal(TenantContext.create("tal_dahan", tmp_path))
    assert journal.started_on() is None
    journal.roll_session(date(2026, 10, 19), 100_000.0)
    journal.roll_session(date(2026, 10, 20), 101_000.0)
    assert journal.started_on() == date(2026, 10, 19)


def test_existing_journal_backfills_its_start_from_the_first_trade(tmp_path):
    tenant = TenantContext.create("tal_dahan", tmp_path)
    Journal(tenant).close()
    conn = sqlite3.connect(tenant.journal_path)
    conn.execute("ALTER TABLE risk_state DROP COLUMN started_on")
    conn.execute(
        "INSERT INTO risk_state (client_id, day, updated_at) VALUES ('tal_dahan', '2026-10-01', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.execute(
        "INSERT INTO trades (client_id, symbol, setup, side, qty, entry_price, stop_price,"
        " opened_at) VALUES ('tal_dahan', 'RIVN', 'breakout', 'buy', 1, 20, 18,"
        " '2026-08-14T15:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    assert Journal(tenant).started_on() == date(2026, 8, 14)

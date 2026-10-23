from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from tradesys.core.enums import DirectiveAction, DirectiveScope, SetupType, Side
from tradesys.core.models import (
    Candidate,
    DayStats,
    FeedbackDirective,
    OrderRequest,
    OrderResult,
    TradeRecord,
)
from tradesys.core.tenancy import TenantContext

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id     TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    setup         TEXT NOT NULL,
    side          TEXT NOT NULL,
    qty           INTEGER NOT NULL,
    entry_price   REAL NOT NULL,
    stop_price    REAL NOT NULL,
    target_price  REAL,
    opened_at     TEXT NOT NULL,
    exit_price    REAL,
    closed_at     TEXT,
    realized_pnl  REAL,
    rationale     TEXT NOT NULL DEFAULT '',
    reflected     INTEGER NOT NULL DEFAULT 0,
    initial_stop  REAL,
    peak_price    REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_client_open ON trades (client_id, closed_at);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    broker_order_id TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    status          TEXT NOT NULL,
    submitted_at    TEXT NOT NULL,
    payload         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS directives (
    directive_id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    TEXT NOT NULL,
    action       TEXT NOT NULL,
    scope        TEXT NOT NULL,
    symbols      TEXT NOT NULL,
    setups       TEXT NOT NULL,
    weight       REAL NOT NULL,
    source_text  TEXT NOT NULL,
    trade_id     INTEGER,
    created_at   TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_directives_client ON directives (client_id, active);

CREATE TABLE IF NOT EXISTS risk_state (
    client_id      TEXT PRIMARY KEY,
    day            TEXT NOT NULL,
    peak_equity    REAL NOT NULL DEFAULT 0,
    day_start_equity REAL NOT NULL DEFAULT 0,
    capital_base   REAL NOT NULL DEFAULT 0,
    started_on     TEXT NOT NULL DEFAULT '',
    realized_pnl   REAL NOT NULL DEFAULT 0,
    trades_opened  INTEGER NOT NULL DEFAULT 0,
    halted         INTEGER NOT NULL DEFAULT 0,
    halt_reason    TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  TEXT NOT NULL,
    agent      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    symbol     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_events ON agent_events (client_id, agent, created_at);
"""


class Journal:
    """Tenant-scoped SQLite journal: trades, orders, directives and risk state.

    One database file per tenant (see TenantContext.journal_path). Every statement
    additionally filters on client_id, so a mis-pointed file cannot leak rows.
    """

    def __init__(self, tenant: TenantContext) -> None:
        self._tenant = tenant
        self.client_id = tenant.client_id
        # check_same_thread=False plus the lock below lets a PositionMonitor poll from
        # its own thread while the trading loop writes from the main one. WAL keeps
        # readers from blocking the writer.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            tenant.journal_path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Additive migrations for journals created by an earlier version."""
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(trades)")
        }
        if "target_price" not in columns:
            self._execute("ALTER TABLE trades ADD COLUMN target_price REAL")
        for column in ("initial_stop", "peak_price"):
            if column not in columns:
                self._execute(f"ALTER TABLE trades ADD COLUMN {column} REAL")
        if "exit_reason" not in columns:
            self._execute("ALTER TABLE trades ADD COLUMN exit_reason TEXT NOT NULL DEFAULT ''")

        risk_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(risk_state)")
        }
        if "capital_base" not in risk_columns:
            self._execute(
                "ALTER TABLE risk_state ADD COLUMN capital_base REAL NOT NULL DEFAULT 0"
            )
        if "started_on" not in risk_columns:
            self._execute(
                "ALTER TABLE risk_state ADD COLUMN started_on TEXT NOT NULL DEFAULT ''"
            )
            # A journal that already traded started on its first trade, not today.
            self._execute(
                "UPDATE risk_state SET started_on = COALESCE("
                "(SELECT substr(MIN(opened_at), 1, 10) FROM trades"
                " WHERE trades.client_id = risk_state.client_id), '')"
            )

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- trades

    def open_trade(
        self,
        candidate: Candidate,
        order: OrderRequest,
        fill_price: float,
        opened_at: datetime,
    ) -> int:
        self._tenant.assert_owns(order)
        cursor = self._execute(
            "INSERT INTO trades (client_id, symbol, setup, side, qty, entry_price, "
            "stop_price, target_price, opened_at, rationale, initial_stop, peak_price) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.client_id,
                order.symbol,
                candidate.setup.value,
                order.side.value,
                order.qty,
                fill_price,
                candidate.stop,
                candidate.target,
                opened_at.isoformat(),
                candidate.rationale,
                candidate.stop,
                fill_price,
            ),
        )
        return int(cursor.lastrowid)

    def trail_stop(
        self,
        trade_id: int,
        stop: float,
        peak: float,
        *,
        release_target: bool = False,
    ) -> TradeRecord | None:
        """Raise a long trade's stop and record its peak. A stop never moves down.

        The MAX() is in the statement itself, so no caller — and no race between
        the monitor thread and the trading loop — can loosen a stop.
        """
        target_sql = ", target_price=NULL" if release_target else ""
        self._execute(
            "UPDATE trades SET stop_price=MAX(stop_price, ?), "
            "peak_price=MAX(COALESCE(peak_price, entry_price), ?)"
            f"{target_sql} "
            "WHERE trade_id=? AND client_id=? AND closed_at IS NULL AND side=?",
            (stop, peak, trade_id, self.client_id, Side.BUY.value),
        )
        return self.get_trade(trade_id)

    def set_target(self, trade_id: int, target: float) -> TradeRecord | None:
        """Give an open trade a profit target (one whose target an earlier rule released)."""
        self._execute(
            "UPDATE trades SET target_price=? WHERE trade_id=? AND client_id=? AND closed_at IS NULL",
            (target, trade_id, self.client_id),
        )
        return self.get_trade(trade_id)

    def close_trade(
        self, trade_id: int, exit_price: float, closed_at: datetime, reason: str = ""
    ) -> TradeRecord | None:
        """Close an open trade and book its P&L. Returns None if it is already closed.

        The read-modify-write is held under one lock so a monitor thread and the
        trading loop cannot book the same exit twice.
        """
        with self._lock:
            row = self._execute(
                "SELECT * FROM trades WHERE trade_id=? AND client_id=? "
                "AND closed_at IS NULL",
                (trade_id, self.client_id),
            ).fetchone()
            if row is None:
                return None
            direction = 1 if row["side"] == Side.BUY.value else -1
            pnl = (exit_price - row["entry_price"]) * row["qty"] * direction
            self._execute(
                "UPDATE trades SET exit_price=?, closed_at=?, realized_pnl=?, exit_reason=? "
                "WHERE trade_id=? AND client_id=?",
                (exit_price, closed_at.isoformat(), pnl, reason, trade_id, self.client_id),
            )
            self._bump_realized_pnl(pnl, closed_at)
            return self.get_trade(trade_id)

    def split_close(
        self, trade_id: int, qty: int, exit_price: float, closed_at: datetime, reason: str
    ) -> TradeRecord | None:
        """Close part of an open trade: the sold shares become their own closed trade.

        Every statistic built on closed trades (daily loss, R-multiples, the
        reflection loop) then sees the partial exit like any other, and the open
        trade keeps its stop for the shares still held.
        """
        with self._lock:
            row = self._execute(
                "SELECT * FROM trades WHERE trade_id=? AND client_id=? AND closed_at IS NULL",
                (trade_id, self.client_id),
            ).fetchone()
            if row is None or not 0 < qty < row["qty"]:
                return None
            direction = 1 if row["side"] == Side.BUY.value else -1
            pnl = (exit_price - row["entry_price"]) * qty * direction
            cursor = self._execute(
                "INSERT INTO trades (client_id, symbol, setup, side, qty, entry_price, "
                "stop_price, target_price, opened_at, rationale, initial_stop, peak_price, "
                "exit_price, closed_at, realized_pnl, exit_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.client_id, row["symbol"], row["setup"], row["side"], qty,
                    row["entry_price"], row["stop_price"], row["target_price"], row["opened_at"],
                    row["rationale"], row["initial_stop"], row["peak_price"],
                    exit_price, closed_at.isoformat(), pnl, reason,
                ),
            )
            self._execute(
                "UPDATE trades SET qty=qty-? WHERE trade_id=? AND client_id=?",
                (qty, trade_id, self.client_id),
            )
            self._bump_realized_pnl(pnl, closed_at)
            return self.get_trade(int(cursor.lastrowid))

    # ---------------------------------------------------------- agent events

    def log_events(self, events: Sequence[tuple[str, str, str, str]], when: datetime) -> None:
        """Record what each agent did: (agent, kind, symbol, detail) rows.

        The cycle report lives only in memory and in a log line; without this the
        operator's agent report could only count trades, never the decisions behind
        them — how many ideas were found, or why the risk manager said no.
        """
        if not events:
            return
        self._executemany(
            "INSERT INTO agent_events (client_id, agent, kind, symbol, detail, created_at)"
            " VALUES (?,?,?,?,?,?)",
            [
                (self.client_id, agent, kind, symbol, detail[:500], when.isoformat())
                for agent, kind, symbol, detail in events
            ],
        )

    def agent_events(self, since: datetime) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT agent, kind, symbol, detail, created_at FROM agent_events"
            " WHERE client_id=? AND created_at >= ? ORDER BY event_id",
            (self.client_id, since.isoformat()),
        ).fetchall()
        return [dict(row) for row in rows]

    def order_statuses(self) -> list[str]:
        rows = self._execute(
            "SELECT status FROM orders WHERE client_id=?", (self.client_id,)
        ).fetchall()
        return [row["status"] for row in rows]

    def get_trade(self, trade_id: int) -> TradeRecord | None:
        row = self._execute(
            "SELECT * FROM trades WHERE trade_id=? AND client_id=?",
            (trade_id, self.client_id),
        ).fetchone()
        return _row_to_trade(row) if row else None

    def open_trades(self) -> list[TradeRecord]:
        rows = self._execute(
            "SELECT * FROM trades WHERE client_id=? AND closed_at IS NULL "
            "ORDER BY trade_id",
            (self.client_id,),
        ).fetchall()
        return [_row_to_trade(r) for r in rows]

    def closed_trades(self, limit: int = 200) -> list[TradeRecord]:
        """Most recently closed trades first. Read-only; used for reporting."""
        rows = self._execute(
            "SELECT * FROM trades WHERE client_id=? AND closed_at IS NOT NULL "
            "ORDER BY closed_at DESC, trade_id DESC LIMIT ?",
            (self.client_id, limit),
        ).fetchall()
        return [_row_to_trade(r) for r in rows]

    def unreflected_closed_trades(self, limit: int = 50) -> list[TradeRecord]:
        rows = self._execute(
            "SELECT * FROM trades WHERE client_id=? AND closed_at IS NOT NULL "
            "AND reflected=0 ORDER BY closed_at LIMIT ?",
            (self.client_id, limit),
        ).fetchall()
        return [_row_to_trade(r) for r in rows]

    def mark_reflected(self, trade_ids: Iterable[int]) -> None:
        self._executemany(
            "UPDATE trades SET reflected=1 WHERE trade_id=? AND client_id=?",
            [(tid, self.client_id) for tid in trade_ids],
        )

    # ---------------------------------------------------------------- orders

    def record_order(self, order: OrderRequest, result: OrderResult) -> None:
        self._tenant.assert_owns(order)
        self._tenant.assert_owns(result)
        self._execute(
            "INSERT OR REPLACE INTO orders (client_order_id, client_id, broker_order_id,"
            " symbol, side, qty, status, submitted_at, payload) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                order.client_order_id,
                self.client_id,
                result.broker_order_id,
                order.symbol,
                order.side.value,
                order.qty,
                result.status.value,
                (result.submitted_at or datetime.now(timezone.utc)).isoformat(),
                json.dumps(
                    {
                        "order_type": order.order_type.value,
                        "limit_price": order.limit_price,
                        "stop_loss": order.stop_loss,
                        "take_profit": order.take_profit,
                    }
                ),
            ),
        )

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent order submissions first. Read-only; used for reporting."""
        rows = self._execute(
            "SELECT client_order_id, broker_order_id, symbol, side, qty, status,"
            " submitted_at FROM orders WHERE client_id=?"
            " ORDER BY submitted_at DESC LIMIT ?",
            (self.client_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def order_exists(self, client_order_id: str) -> bool:
        row = self._execute(
            "SELECT 1 FROM orders WHERE client_order_id=? AND client_id=?",
            (client_order_id, self.client_id),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------ directives

    def save_directives(self, directives: Sequence[FeedbackDirective]) -> list[int]:
        ids: list[int] = []
        for directive in directives:
            self._tenant.assert_owns(directive)
            cursor = self._execute(
                "INSERT INTO directives (client_id, action, scope, symbols, setups,"
                " weight, source_text, trade_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    self.client_id,
                    directive.action.value,
                    directive.scope.value,
                    json.dumps(list(directive.symbols)),
                    json.dumps([s.value for s in directive.setups]),
                    directive.weight,
                    directive.source_text,
                    directive.trade_id,
                    (directive.created_at or datetime.now(timezone.utc)).isoformat(),
                ),
            )
            ids.append(int(cursor.lastrowid))
        return ids

    def active_directives(self) -> list[FeedbackDirective]:
        rows = self._execute(
            "SELECT * FROM directives WHERE client_id=? AND active=1 "
            "ORDER BY directive_id DESC",
            (self.client_id,),
        ).fetchall()
        return [
            FeedbackDirective(
                client_id=row["client_id"],
                action=DirectiveAction(row["action"]),
                scope=DirectiveScope(row["scope"]),
                symbols=tuple(json.loads(row["symbols"])),
                setups=tuple(SetupType(s) for s in json.loads(row["setups"])),
                weight=row["weight"],
                source_text=row["source_text"],
                trade_id=row["trade_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                directive_id=row["directive_id"],
            )
            for row in rows
        ]

    def retire_directive(self, directive_id: int) -> bool:
        cursor = self._execute(
            "UPDATE directives SET active=0 WHERE directive_id=? AND client_id=?",
            (directive_id, self.client_id),
        )
        return cursor.rowcount > 0

    # ------------------------------------------------------------ risk state

    def _risk_row(self, today: date) -> sqlite3.Row:
        row = self._execute(
            "SELECT * FROM risk_state WHERE client_id=?", (self.client_id,)
        ).fetchone()
        if row is None:
            self._execute(
                "INSERT INTO risk_state (client_id, day, updated_at) VALUES (?,?,?)",
                (self.client_id, today.isoformat(), datetime.now(timezone.utc).isoformat()),
            )
            return self._risk_row(today)
        return row

    def roll_session(self, today: date, equity: float) -> DayStats:
        """Reset daily counters on a new trading day and track peak equity."""
        row = self._risk_row(today)
        peak = max(float(row["peak_equity"]), equity)
        if row["day"] != today.isoformat():
            self._execute(
                "UPDATE risk_state SET day=?, realized_pnl=0, trades_opened=0,"
                " day_start_equity=?, peak_equity=?, updated_at=? WHERE client_id=?",
                (
                    today.isoformat(),
                    equity,
                    peak,
                    datetime.now(timezone.utc).isoformat(),
                    self.client_id,
                ),
            )
        else:
            start_equity = float(row["day_start_equity"]) or equity
            self._execute(
                "UPDATE risk_state SET peak_equity=?, day_start_equity=?, updated_at=?"
                " WHERE client_id=?",
                (
                    peak,
                    start_equity,
                    datetime.now(timezone.utc).isoformat(),
                    self.client_id,
                ),
            )
        # The first equity ever seen becomes the capital base the floor is
        # measured against. Set once, never moved by later gains or losses.
        self._execute(
            "UPDATE risk_state SET capital_base=? WHERE client_id=? AND capital_base=0",
            (equity, self.client_id),
        )
        # Same for the day trading began: the benchmark comparison starts here.
        self._execute(
            "UPDATE risk_state SET started_on=? WHERE client_id=? AND started_on=''",
            (today.isoformat(), self.client_id),
        )
        return self.day_stats(today)

    def day_stats(self, today: date) -> DayStats:
        row = self._risk_row(today)
        return DayStats(
            client_id=self.client_id,
            day=date.fromisoformat(row["day"]),
            realized_pnl=float(row["realized_pnl"]),
            trades_opened=int(row["trades_opened"]),
            peak_equity=float(row["peak_equity"]),
        )

    def capital_base(self) -> float:
        """Equity when the tenant first traded; 0 until the first session."""
        row = self._execute(
            "SELECT capital_base FROM risk_state WHERE client_id=?", (self.client_id,)
        ).fetchone()
        return float(row["capital_base"]) if row else 0.0

    def started_on(self) -> date | None:
        """The first trading day this tenant was seen; None before the first session."""
        row = self._execute(
            "SELECT started_on FROM risk_state WHERE client_id=?", (self.client_id,)
        ).fetchone()
        return date.fromisoformat(row["started_on"]) if row and row["started_on"] else None

    def open_risk(self) -> float:
        """What the book loses if every open position hits its stop at once."""
        return sum(
            abs(t.entry_price - t.stop_price) * t.qty for t in self.open_trades()
        )

    def day_start_equity(self) -> float:
        row = self._execute(
            "SELECT day_start_equity FROM risk_state WHERE client_id=?",
            (self.client_id,),
        ).fetchone()
        return float(row["day_start_equity"]) if row else 0.0

    def record_deposit(self, amount: float) -> None:
        """Lift every equity reference by a deposit, so new money is not profit.

        Without this, a top-up would read as a gain: it would raise the peak the
        drawdown is measured from only later, inflate the day's result, and make
        the return since start look better than the trading was.
        """
        self._execute(
            "UPDATE risk_state SET peak_equity=peak_equity+?, "
            "day_start_equity=day_start_equity+?, "
            "capital_base=CASE WHEN capital_base>0 THEN capital_base+? ELSE 0 END "
            "WHERE client_id=?",
            (amount, amount, amount, self.client_id),
        )

    def record_trade_opened(self) -> None:
        self._execute(
            "UPDATE risk_state SET trades_opened=trades_opened+1, updated_at=?"
            " WHERE client_id=?",
            (datetime.now(timezone.utc).isoformat(), self.client_id),
        )

    def _bump_realized_pnl(self, pnl: float, when: datetime) -> None:
        self._execute(
            "UPDATE risk_state SET realized_pnl=realized_pnl+?, updated_at=?"
            " WHERE client_id=?",
            (pnl, when.isoformat(), self.client_id),
        )

    def halt(self, reason: str) -> None:
        self._execute(
            "UPDATE risk_state SET halted=1, halt_reason=?, updated_at=? WHERE client_id=?",
            (reason, datetime.now(timezone.utc).isoformat(), self.client_id),
        )

    def clear_halt(self, rebase_peak_to: float | None = None) -> None:
        """Lift a halt. `rebase_peak_to` restarts drawdown measurement from that equity.

        Without the rebase, lifting a drawdown halt while equity is still below the
        old peak is a no-op: the next preflight measures the same drawdown and
        halts again. Releasing a halt means accepting today's equity as the new
        peak, so a fresh drawdown budget starts from here.
        """
        if rebase_peak_to is not None:
            self._execute(
                "UPDATE risk_state SET peak_equity=? WHERE client_id=?",
                (rebase_peak_to, self.client_id),
            )
        self._execute(
            "UPDATE risk_state SET halted=0, halt_reason='', updated_at=? WHERE client_id=?",
            (datetime.now(timezone.utc).isoformat(), self.client_id),
        )

    def halt_state(self) -> tuple[bool, str]:
        row = self._execute(
            "SELECT halted, halt_reason FROM risk_state WHERE client_id=?",
            (self.client_id,),
        ).fetchone()
        if row is None:
            return False, ""
        return bool(row["halted"]), row["halt_reason"]

    # ------------------------------------------------------------ analytics

    def performance_by_setup(self) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT setup, COUNT(*) AS n,"
            " SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,"
            " ROUND(SUM(realized_pnl), 2) AS pnl"
            " FROM trades WHERE client_id=? AND closed_at IS NOT NULL GROUP BY setup",
            (self.client_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _row_to_trade(row: sqlite3.Row) -> TradeRecord:
    return TradeRecord(
        trade_id=row["trade_id"],
        client_id=row["client_id"],
        symbol=row["symbol"],
        setup=SetupType(row["setup"]),
        side=Side(row["side"]),
        qty=row["qty"],
        entry_price=row["entry_price"],
        stop_price=row["stop_price"],
        target_price=row["target_price"],
        opened_at=datetime.fromisoformat(row["opened_at"]),
        exit_price=row["exit_price"],
        closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
        realized_pnl=row["realized_pnl"],
        rationale=row["rationale"],
        initial_stop=row["initial_stop"],
        peak_price=row["peak_price"],
        exit_reason=row["exit_reason"],
    )

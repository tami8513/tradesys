"""A message to the client's phone for every buy and every sale, in plain Hebrew.

The feed reads the journal since the last message it sent, so a restart sends
nothing twice and a quiet day sends nothing at all. The first run starts from
now rather than replaying the whole history into the phone.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from tradesys.clients.statement import Line, statement
from tradesys.core.fileio import write_json_atomic
from tradesys.core.models import TradeRecord
from tradesys.orchestration.agent_report import EXIT_NAMES, SETUP_NAMES
from tradesys.orchestration.why import explain_rationale

FEED_FILE = "trade_feed.json"


def _label(symbol: str, names: Callable[[str], str | None]) -> str:
    name = names(symbol)
    return f"{symbol} · {name}" if name else symbol


def bought(trade: TradeRecord, names: Callable[[str], str | None]) -> str:
    cost = trade.qty * trade.entry_price
    stop = trade.initial_stop if trade.initial_stop is not None else trade.stop_price
    lines = [
        f"🟢 קנייה · {_label(trade.symbol, names)}",
        f"{trade.qty:,} מניות × ${trade.entry_price:,.2f} = ${cost:,.0f}",
        f"🛡 הגנה: ${stop:,.2f} ({(stop / trade.entry_price - 1) * 100:+.1f}%)",
    ]
    if trade.target_price:
        lines.append(f"🎯 יעד: ${trade.target_price:,.2f} ({(trade.target_price / trade.entry_price - 1) * 100:+.1f}%)")
    lines.append(f"💡 למה: {SETUP_NAMES.get(trade.setup.value, trade.setup.value)}"
                 + (f" · {explain_rationale(trade.rationale)}" if trade.rationale else ""))
    lines.append(f"ההפסד המקסימלי אם ההגנה תופעל: ${trade.qty * (trade.entry_price - stop):,.0f}")
    return "\n".join(lines)


def sold(trade: TradeRecord, names: Callable[[str], str | None], line: Line | None = None) -> str:
    pnl = trade.realized_pnl or 0.0
    pct = ((trade.exit_price or 0) / trade.entry_price - 1) * 100
    reason = trade.exit_reason
    label = EXIT_NAMES.get(reason, "יציאה")
    if reason == "stop_loss" and pnl > 0:
        label = "ההגנה הנגררת נעלה רווח"
    elif reason == "stop_loss":
        label = "ההגנה הופעלה ועצרה את ההפסד"
    if reason == "earnings_trim":
        head = f"✂️ מכרתי חלק · {_label(trade.symbol, names)}"
    elif pnl >= 0:
        head = f"✅ מכירה ברווח · {_label(trade.symbol, names)}"
    else:
        head = f"🔻 מכירה בהפסד · {_label(trade.symbol, names)}"
    held = (trade.closed_at - trade.opened_at).days if trade.closed_at else 0
    lines = [
        head,
        f"{trade.qty:,} מניות × ${trade.exit_price or 0:,.2f}",
        f"{'רווח' if pnl >= 0 else 'הפסד'}: ${pnl:+,.0f} ({pct:+.1f}%) · {trade.r_multiple():+.1f}R",
    ]
    if line is not None:
        tax = f"מס ${line.tax:,.0f}" if line.tax >= 0 else f"החזר מס ${-line.tax:,.0f}"
        lines.append(f"💵 נטו לכיס: ${line.take_home:+,.0f} (עמלות ${line.commissions:,.0f}, "
                     f"דמי הצלחה ${line.fee:,.0f}, {tax})")
    lines += [f"📌 למה: {label}", f"⏱ הוחזקה {held} ימים"]
    return "\n".join(lines)


def index_move(event: dict) -> str:
    qty, _, rest = event["detail"].partition("|")
    price, _, reason = rest.partition("|")
    verb = "📈 רובוט המדדים קנה" if event["kind"] == "buy" else "📉 רובוט המדדים מכר"
    return f"{verb} {qty} יחידות {event['symbol']} ב-${float(price or 0):,.2f}\n💡 {reason}"


def read_cursor(tenant_dir: Path) -> datetime | None:
    path = Path(tenant_dir) / FEED_FILE
    if not path.exists():
        return None
    try:
        return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["since"])
    except (ValueError, OSError, KeyError, TypeError):
        return None


def new_messages(journal, tenant_dir: Path, now: datetime,
                 names: Callable[[str], str | None] = lambda s: None) -> list[str]:
    """Messages for everything bought or sold since the last call, oldest first."""
    since = read_cursor(tenant_dir)
    write_json_atomic(Path(tenant_dir) / FEED_FILE, {"since": now.isoformat()})
    if since is None:
        return []
    closed = journal.closed_trades(limit=500)
    booked = {line.trade_id: line for year in statement(closed) for line in year.lines}
    timed: list[tuple[datetime, str]] = []
    for trade in journal.open_trades() + closed:
        # A split-off part keeps the original's opening time, so it is never "bought" twice.
        if since < trade.opened_at <= now and trade.exit_reason != "earnings_trim":
            timed.append((trade.opened_at, bought(trade, names)))
    for trade in closed:
        if trade.closed_at and since < trade.closed_at <= now:
            timed.append((trade.closed_at, sold(trade, names, booked.get(trade.trade_id))))
    for event in journal.agent_events(since):
        if event["agent"] == "index" and event["kind"] in ("buy", "sell"):
            when = datetime.fromisoformat(event["created_at"])
            if since < when <= now:
                timed.append((when, index_move(event)))
    return [text for _, text in sorted(timed, key=lambda item: item[0])]

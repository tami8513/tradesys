"""The daily briefing: today's trading in a few plain Hebrew sentences.

Built only from what was recorded today — fills, exits, stop raises, refusals —
so it can never describe a trade that did not happen. Each sentence names the
stock and the reason, the way a personal portfolio manager would.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

from tradesys.core.models import TradeRecord
from tradesys.orchestration.agent_report import EXIT_NAMES, REASON_SEP, SETUP_NAMES
from tradesys.orchestration.why import explain_reason


@dataclass(frozen=True, slots=True)
class Brief:
    headline: str
    lines: list[str]


def _money(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.0f}"


def daily_brief(
    events: Sequence[dict[str, Any]],
    closed_today: Sequence[TradeRecord],
    open_trades: Sequence[TradeRecord],
    day_pnl: float | None,
    equity: float,
) -> Brief:
    """`events` and `closed_today` cover the current trading day only."""
    if day_pnl is None or (not events and not closed_today):
        headline = "יום שקט: עוד לא היו החלטות מסחר היום"
    elif abs(day_pnl) < 0.5:
        headline = "היום: ללא שינוי בשווי התיק"
    else:
        pct = day_pnl / (equity - day_pnl) * 100 if equity - day_pnl else 0.0
        headline = f"היום: {_money(day_pnl)} ({pct:+.2f}%)"

    lines: list[str] = []
    setups = {
        e["symbol"]: e["detail"].partition("|")[0]
        for e in events
        if e["agent"] == "scout" and e["kind"] == "candidate"
    }
    for event in events:
        if event["agent"] == "execution" and event["kind"] == "filled":
            setup = SETUP_NAMES.get(setups.get(event["symbol"], ""), "")
            lines.append(f"נכנסנו ל-{event['symbol']}" + (f" ({setup})" if setup else ""))

    for trade in closed_today:
        reason = EXIT_NAMES.get(trade.exit_reason, "יציאה")
        if trade.exit_reason == "stop_loss" and (trade.realized_pnl or 0) > 0:
            reason = "סטופ נגרר שנעל רווח"
        lines.append(
            f"יצאנו מ-{trade.symbol} ב{reason}: {_money(trade.realized_pnl or 0)} "
            f"({trade.r_multiple():+.2f}R)"
        )

    protected = sorted(
        t.symbol for t in open_trades if t.side.value == "buy" and t.stop_price >= t.entry_price
    )
    if protected:
        lines.append(
            f"הסטופ של {', '.join(protected)} כבר מעל מחיר הכניסה, כך שהעסקה לא יכולה להפסיד"
        )

    refusals = [e for e in events if e["agent"] == "risk" and e["kind"] == "rejected"]
    if refusals:
        reasons = Counter(
            explain_reason(r)
            for e in refusals
            for r in e["detail"].split(REASON_SEP)[:1]
            if r
        )
        top, _ = reasons.most_common(1)[0]
        lines.append(f"שומר הסיכון חסם {len(refusals)} רעיונות. הסיבה העיקרית: {top}")

    halts = [e for e in events if e["agent"] == "risk" and e["kind"] == "halted"]
    if halts:
        lines.append(f"המסחר נעצר: {explain_reason(halts[-1]['detail'])}")

    at_risk = sum(
        max(0.0, (t.entry_price - t.stop_price) * t.qty) for t in open_trades if t.side.value == "buy"
    )
    if open_trades:
        lines.append(
            f"מחזיקים {len(open_trades)} פוזיציות. אם כל הסטופים יופעלו, "
            f"ההפסד המרבי מכאן הוא {_money(-at_risk) if at_risk else '$0'}"
        )
    elif events:
        lines.append("אין פוזיציות פתוחות. המערכת ממתינה להזדמנות שעוברת את כל הבדיקות")
    return Brief(headline, lines)

"""The morning question: before the bell, ask the client about positions worth a decision.

A position that is well in profit or close to its target is the one moment a
client's view adds something the method does not have: raise the target and
let it run, take the profit at the open, or leave it to the robot. No answer
means the robot carries on by its rules. Stop-loss exits are never put to a
question: a stop that waits for an answer is not a stop.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Callable

from tradesys.core.fileio import locked, write_json_atomic
from tradesys.core.models import TradeRecord

QUESTIONS_FILE = "morning_questions.json"
ASK_AT_R = 1.5               # profit, in multiples of the risk taken
ASK_NEAR_TARGET = 0.8        # share of the way from entry to target
CHOICES = {
    "run": "🚀 להעלות יעד ולתת לה לרוץ",
    "take": "💰 לממש את הרווח בפתיחה",
    "auto": "🤖 שהרובוט יחליט",
}
CONFIRMATIONS = {
    "run": "היעד הוסר: המניה ממשיכה לרוץ עם ההגנה הנגררת",
    "take": "בפתיחת המסחר הרובוט ימכור ויממש את הרווח",
    "auto": "הרובוט ימשיך לפי השיטה שלו",
}


def worth_asking(trade: TradeRecord, price: float) -> bool:
    risk = trade.risk_per_share
    gain_r = (price - trade.entry_price) / risk if risk > 0 else 0.0
    near_target = (
        trade.target_price is not None
        and price >= trade.entry_price + ASK_NEAR_TARGET * (trade.target_price - trade.entry_price)
    )
    return price > trade.entry_price and (gain_r >= ASK_AT_R or near_target)


def question_text(trade: TradeRecord, price: float, names: Callable[[str], str | None]) -> str:
    name = names(trade.symbol)
    label = f"{trade.symbol} · {name}" if name else trade.symbol
    gain = (price - trade.entry_price) * trade.qty
    lines = [
        f"☀️ {label}",
        f"עלתה {(price / trade.entry_price - 1) * 100:+.1f}% מאז הקנייה "
        f"(${trade.entry_price:,.2f} ← ${price:,.2f})",
        f"הרווח כרגע: ${gain:+,.0f}"
        + (f" ({(price - trade.entry_price) / trade.risk_per_share:+.1f}R)" if trade.risk_per_share else ""),
    ]
    if trade.target_price:
        lines.append(f"🎯 היעד: ${trade.target_price:,.2f} (עוד {(trade.target_price / price - 1) * 100:+.1f}%)")
    else:
        lines.append("🎯 בלי יעד: המניה כבר רצה עם ההגנה הנגררת")
    locked_in = (trade.stop_price - trade.entry_price) * trade.qty
    lines.append(f"🛡 ההגנה כרגע: ${trade.stop_price:,.2f}"
                 + (f" (נועלת ${locked_in:+,.0f})" if locked_in > 0 else ""))
    lines.append("מה לעשות בפתיחה? אם לא תענה, הרובוט ממשיך לפי השיטה.")
    return "\n".join(lines)


def _read(tenant_dir: Path) -> dict:
    path = Path(tenant_dir) / QUESTIONS_FILE
    if not path.exists():
        return {"day": "", "questions": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"day": "", "questions": []}


def asked_on(tenant_dir: Path) -> str:
    return _read(tenant_dir).get("day", "")


def ask(tenant_dir: Path, day: date, trades: list[TradeRecord], prices: dict[str, float],
        names: Callable[[str], str | None] = lambda s: None) -> list[dict]:
    """Today's questions, saved so an answer can find them. Asked once a day."""
    questions = [
        {"id": f"{day.isoformat()}-{t.trade_id}", "trade_id": t.trade_id, "symbol": t.symbol,
         "text": question_text(t, prices[t.symbol], names), "answer": None, "applied": False}
        for t in trades if t.symbol in prices and worth_asking(t, prices[t.symbol])
    ]
    path = Path(tenant_dir) / QUESTIONS_FILE
    with locked(path):
        write_json_atomic(path, {"day": day.isoformat(), "questions": questions})
    return questions


def answer(tenant_dir: Path, question_id: str, choice: str) -> str | None:
    """Record the client's choice; None when the question is gone or already settled."""
    if choice not in CHOICES:
        return None
    path = Path(tenant_dir) / QUESTIONS_FILE
    with locked(path):
        book = _read(tenant_dir)
        for question in book["questions"]:
            if question["id"] == question_id and not question["applied"]:
                question["answer"] = choice
                write_json_atomic(path, book)
                return f"{question['symbol']}: {CONFIRMATIONS[choice]}"
    return None


def pending(tenant_dir: Path) -> list[dict]:
    """Questions still open: unanswered, or answered but not yet carried out."""
    return [q for q in _read(tenant_dir)["questions"] if not q["applied"]]


def mark_applied(tenant_dir: Path, question_id: str) -> None:
    path = Path(tenant_dir) / QUESTIONS_FILE
    with locked(path):
        book = _read(tenant_dir)
        for question in book["questions"]:
            if question["id"] == question_id:
                question["applied"] = True
        write_json_atomic(path, book)


def carry_out(session) -> list[tuple[str, str, str, str]]:
    """Act on today's answers at the first cycle after the open. Returns agent events."""
    events = []
    data_dir = session.tenant.data_dir
    for question in pending(data_dir):
        choice = question["answer"]
        if choice is None:
            continue
        trade = session.journal.get_trade(question["trade_id"])
        if trade is not None and trade.closed_at is None:
            if choice == "run":
                session.journal.trail_stop(trade.trade_id, trade.stop_price,
                                           trade.peak_price or trade.entry_price, release_target=True)
                events.append(("client", "let_run", trade.symbol, ""))
            elif choice == "take":
                report = session.monitor.close_manually(trade.symbol, "client: take the profit")
                events.append(("client", "take_profit", trade.symbol, ""))
                events.extend(("monitor", "exit", c.trade.symbol, f"manual|{c.trade.r_multiple():.2f}")
                              for c in report.closed)
        mark_applied(data_dir, question["id"])
    return events

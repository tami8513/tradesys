"""Watchdog: checks each tenant's book against the broker and tells the operator.

Until now problems were found only when someone asked (an expired stop was found
that way). This runs inside the scheduler, after every loop, and turns findings
into short Hebrew alerts sent to the operator's phone.

It cannot report its own death: if the server is down, nothing here runs. That
is what an outside uptime check on the health URL is for.

Alerts are kept in a small book so a standing problem is announced once, repeated
every few hours while it lasts, and closed with a "resolved" message.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Protocol

from tradesys.clients.index_sleeve import read_ledger
from tradesys.clients.proposals import pending
from tradesys.orchestration.index_desk import book_account
from tradesys.orchestration.telegram import TelegramBot
from tradesys.core.fileio import locked, write_json_atomic

log = logging.getLogger("tradesys.watchdog")

REPEAT_AFTER = timedelta(hours=6)
DRAWDOWN_WARNING_SHARE = 0.75
DAILY_DROP_PCT = 7.0
ICONS = {"critical": "🔴", "warning": "🟠", "info": "🟡"}


@dataclass(frozen=True, slots=True)
class Alert:
    key: str        # stable id, so the same problem is not announced twice
    level: str      # critical | warning | info
    text: str


def inspect(session) -> list[Alert]:
    """Everything wrong with one tenant right now. Reads only; changes nothing."""
    alerts: list[Alert] = []
    journal, broker = session.journal, session.broker
    open_trades = {t.symbol.upper(): t for t in journal.open_trades()}
    held = {p.symbol.upper(): p for p in broker.positions()}

    # The index robot's funds are held on purpose, without a stop, by its own book.
    index_held = set(read_ledger(session.tenant.data_dir).holdings)
    for symbol in sorted(set(held) - set(open_trades) - index_held):
        alerts.append(Alert(
            f"orphan:{symbol}", "critical",
            f"{symbol} מוחזקת אצל הברוקר אבל לא רשומה במערכת, ולכן אף סוכן לא מנהל אותה",
        ))
    for symbol in sorted(set(open_trades) - set(held)):
        alerts.append(Alert(
            f"missing:{symbol}", "warning",
            f"{symbol} רשומה כפתוחה אבל לא נמצאת אצל הברוקר; סוכן היציאה יסגור אותה ביומן",
        ))

    resting_stop = getattr(broker, "resting_stop", None)
    if resting_stop is not None:
        for symbol in sorted(set(held) & set(open_trades)):
            if resting_stop(symbol) is None:
                alerts.append(Alert(
                    f"unprotected:{symbol}", "critical",
                    f"ל-{symbol} אין פקודת הגנה אצל הברוקר; אם השרת ייפול, אין סטופ",
                ))

    for proposal in pending(session.tenant.data_dir, datetime.now(timezone.utc)):
        alerts.append(Alert(
            f"proposal:{proposal.proposal_id}", "info",
            f"✋ הרובוט רוצה לקנות {proposal.qty} מניות של {proposal.symbol} בסביבות "
            f"${proposal.entry:,.2f}, עם הגנה ב-${proposal.stop:,.2f}. אשר או דחה באתר "
            "עד סוף יום המסחר.",
        ))

    halted, reason = journal.halt_state()
    if halted:
        alerts.append(Alert("halted", "warning", f"המסחר עצור: {reason}"))

    account = book_account(session)
    peak = journal.day_stats(date.today()).peak_equity
    limit = session.profile.risk.max_drawdown_pct
    if peak > 0 and not halted:
        drawdown = max(peak - account.equity, 0.0) / peak * 100
        if drawdown >= limit * DRAWDOWN_WARNING_SHARE:
            alerts.append(Alert(
                "drawdown", "warning",
                f"ירידה של {drawdown:.1f}% מהשיא, קרוב למגבלת העצירה ({limit:g}%)",
            ))

    for symbol in sorted(held):
        try:
            bars = session.market_data.history(symbol, timeframe="1Day", limit=2)
        except Exception:  # a data outage is not itself an alert about the book
            continue
        if len(bars) == 2 and bars[0].close:
            change = (bars[1].close / bars[0].close - 1) * 100
            if change <= -DAILY_DROP_PCT:
                alerts.append(Alert(
                    f"drop:{symbol}:{bars[1].ts.date().isoformat()}", "info",
                    f"{symbol} ירדה היום {change:.1f}%",
                ))
    return alerts


def cycle_failure(client_id: str, outcome: str) -> Alert | None:
    if not outcome.startswith("failed:"):
        return None
    return Alert("cycle_failed", "critical", f"סבב המסחר נכשל: {outcome[len('failed: '):][:200]}")


class AlertBook:
    """What has been announced, so alerts repeat sparingly and close cleanly."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def active(self) -> dict:
        return self._load()

    def update(self, client_id: str, alerts: Iterable[Alert], now: datetime) -> list[str]:
        """Record this tenant's current alerts; return the messages worth sending."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with locked(self._path):
            book = self._load()
            mine = book.get(client_id, {})
            current = {alert.key: alert for alert in alerts}
            messages = []
            for key, alert in current.items():
                entry = mine.get(key)
                last = datetime.fromisoformat(entry["last_sent"]) if entry else None
                if last is None or now - last >= REPEAT_AFTER:
                    prefix = "" if last is None else "עדיין: "
                    messages.append(f"{ICONS[alert.level]} {client_id} · {prefix}{alert.text}")
                    last = now
                mine[key] = {
                    "level": alert.level,
                    "text": alert.text,
                    "since": entry["since"] if entry else now.isoformat(),
                    "last_sent": last.isoformat(),
                }
            for key in [k for k in mine if k not in current]:
                closed = mine.pop(key)
                # A proposal that was answered is not a problem that got fixed.
                if not key.startswith("proposal:"):
                    messages.append(f"✅ {client_id} · נפתר: {closed['text']}")
            book[client_id] = mine
            write_json_atomic(self._path, book)
        return messages


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class LogNotifier:
    """No channel configured: the alert still reaches the server log and the admin."""

    def send(self, text: str) -> None:
        log.warning("ALERT %s", text)


def greet_once(notifier: Notifier, flag: Path) -> None:
    """When a phone channel is set up or changed, say so on it: proof that it works."""
    if isinstance(notifier, LogNotifier):
        return
    fingerprint = getattr(notifier, "fingerprint", "")
    if flag.exists():
        try:
            if json.loads(flag.read_text(encoding="utf-8")).get("fingerprint", "") == fingerprint:
                return
        except ValueError:
            pass
    # Marked only once it actually went out: a greeting that failed (the bot not
    # started yet, a bad token) is tried again on the next start.
    if notifier.send("✅ ההתראות של tradesys מחוברות. מעכשיו תקבל כאן הודעה כשמשהו דורש את תשומת לבך.") is None:
        return
    flag.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(flag, {"greeted_at": datetime.now(timezone.utc).isoformat(), "fingerprint": fingerprint})


def notifier_from_env() -> Notifier:
    # Pasted on a phone, a token often brings a space or a line break with it.
    token = os.getenv("TRADESYS_TELEGRAM__BOT_TOKEN", "").strip().strip('"').strip()
    chat_id = os.getenv("TRADESYS_TELEGRAM__CHAT_ID", "").strip().strip('"').strip()
    return TelegramBot(token, chat_id) if token and chat_id else LogNotifier()

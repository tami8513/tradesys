"""Runs trading cycles by itself, in step with the exchange's opening hours.

    python -m tradesys.scheduler

While the market is open, every TRADESYS_CYCLE_MINUTES (default 15) it runs one
full cycle — monitor sweep, scout, risk, execution, reflection — for each tenant
whose automation switch is on and whose account is not suspended. While closed,
it sleeps until the next open, re-checking at least every half hour.

Failures are contained per tenant and per loop: one tenant's broken broker or a
data outage never stops the others, and never stops the loop.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tradesys.clients.automation import automation_enabled, lifecycle_active
from tradesys.clients.picks import approval_day
from tradesys.core.config import Settings
from tradesys.core.logging import configure
from tradesys.data.market_clock import MarketClock, market_clock
from tradesys.orchestration.engine import TradingSystem
from tradesys.orchestration.morning import CHOICES, answer, ask, asked_on
from tradesys.orchestration.scouting import scout_due, scout_market
from tradesys.orchestration import phone_link
from tradesys.orchestration.telegram import TelegramBot, chat_of, poll_forever, press, question
from tradesys.orchestration.trade_feed import new_messages
from tradesys.orchestration.watchdog import (
    AlertBook,
    Notifier,
    cycle_failure,
    greet_once,
    inspect,
    notifier_from_env,
)

log = logging.getLogger("tradesys.scheduler")

IDLE_RECHECK_SECONDS = 30 * 60
# The morning question goes out in the last 70 minutes before the bell; with an
# idle recheck every 30 minutes at least two checks always fall inside it.
MORNING_WINDOW_SECONDS = 70 * 60
NEW_YORK = ZoneInfo("America/New_York")


def due_tenants(system: TradingSystem) -> list[str]:
    due = []
    for client_id in system.registry.list_ids():
        tenant_dir = system.tenant(client_id).data_dir
        if automation_enabled(tenant_dir, client_id) and lifecycle_active(tenant_dir):
            due.append(client_id)
    return due


def run_once(system: TradingSystem) -> dict[str, str]:
    """One cycle for every due tenant. Returns client_id -> one-line outcome."""
    outcomes: dict[str, str] = {}
    for client_id in due_tenants(system):
        session = None
        try:
            session = system.for_client(client_id)
            report = session.run_cycle()
            outcomes[client_id] = report.summary()
            log.info(report.summary())
        except Exception as exc:
            outcomes[client_id] = f"failed: {type(exc).__name__}: {exc}"
            log.exception("cycle failed for %s", client_id)
        finally:
            if session is not None:
                session.close()
    return outcomes


def asset_names(settings: Settings):
    """Company names for messages; a missing listing just leaves the ticker alone."""
    from tradesys.data.asset_search import AssetDirectory
    from tradesys.data.scouting_feeds import alpaca_assets

    directory = AssetDirectory(
        settings.data_dir / "_platform",
        alpaca_assets(settings.market_data_key_id, settings.market_data_secret_key),
    )

    def name(symbol: str) -> str | None:
        try:
            return directory.name(symbol)
        except Exception:  # a name is decoration; the message goes out without it
            return None

    return name


def ask_morning(session, notifier: Notifier, names) -> None:
    """Once a trading day, before the bell: ask about positions worth a decision."""
    today = datetime.now(NEW_YORK).date()
    if asked_on(session.tenant.data_dir) == today.isoformat():
        return
    trades = session.journal.open_trades()
    prices = {}
    for trade in trades:
        try:
            prices[trade.symbol] = session.market_data.last_price(trade.symbol)
        except Exception:  # an unpriced position is simply not asked about today
            continue
    for pending_question in ask(session.tenant.data_dir, today, trades, prices, names):
        buttons = [[(label, f"m|{session.client_id}|{pending_question['id']}|{key}")]
                   for key, label in CHOICES.items()]
        if isinstance(notifier, TelegramBot):
            notifier.send(pending_question["text"], buttons)
        else:
            notifier.send(pending_question["text"])


HELP = (
    "👋 אני האנליסט של tradesys. אפשר לשאול אותי כל שאלה על שוק ההון, למשל:\n"
    "• מה מצב הנאסד\"ק היום?\n"
    "• תנתח לי את אנבידיה\n"
    "• מה קורה בתיק שלי?\n"
    "• למה מניות השבבים ירדו השבוע?\n"
    "• איך הריבית של הפד משפיעה על מניות צמיחה?\n"
    "אני בודק נתונים חיים ומחפש ברשת כשצריך. אני לא קונה ולא מוכר: את זה עושה הרובוט, "
    "דרך שומר הסיכון. /new מתחיל שיחה חדשה."
)


def ask_expert(system: TradingSystem, bot: TelegramBot, text: str, client_id: str | None = None) -> None:
    """Answer one question with the market expert.

    `client_id` is set for a client's own chat: the expert then sees only that
    client's account, and the conversation and daily cap are theirs. Without it
    the question is the operator's, who sees every account.
    """
    from tradesys.orchestration import market_brain as brain

    memory = system.tenant(client_id).data_dir if client_id else system.settings.data_dir / "_platform"
    if text in ("/start", "/help") or text.startswith("/start "):
        bot.send(HELP)
        return
    if text == "/new":
        brain.forget(memory)
        bot.send("התחלנו שיחה חדשה.")
        return
    if not brain.configured():
        bot.send("כדי שאוכל לענות על שאלות צריך להגדיר ב-Render את ANTHROPIC_API_KEY.")
        return
    today = datetime.now(NEW_YORK).date()
    cap = brain.CLIENT_DAILY_QUESTIONS if client_id else brain.DAILY_QUESTIONS
    if not brain.within_daily_cap(memory, today, cap):
        bot.send(f"הגעת למכסת השאלות להיום ({cap}). נמשיך מחר.")
        return
    bot.send("🔎 בודק… (עד דקה)")
    try:
        reply = brain.answer(system, text, today, client_id)
    except Exception:  # a failed answer must not take the listener down
        log.exception("market expert failed")
        reply = "משהו השתבש בזמן הבדיקה. נסה שוב בעוד דקה."
    for part in brain.split_message(reply):
        bot.send(part)


def answer_button(system: TradingSystem, data: str, client_id: str | None = None) -> str | None:
    """A morning-question button: "m|client_id|question_id|choice".

    From a client's own chat only that client's questions can be answered.
    """
    parts = data.split("|")
    if len(parts) != 4 or parts[0] != "m" or parts[1] not in system.registry.list_ids():
        return None
    if client_id is not None and parts[1] != client_id:
        return None
    return answer(system.tenant(parts[1]).data_dir, parts[2], parts[3])


LINKED = ("✅ הטלגרם שלך מחובר לחשבון {client}. מעכשיו תקבל כאן הודעה על כל קנייה ומכירה, "
          "ואת שאלת הבוקר לפני הפתיחה. אפשר גם לשאול אותי כל שאלה על שוק ההון.")
STRANGER = ("👋 זה הבוט של tradesys. כדי לחבר אותו לחשבון שלך: בפורטל, בלשונית \"📱 טלגרם\", "
            "רשום את מספר הטלפון ולחץ \"חבר\". אחר כך לחץ כאן על הכפתור למטה כדי לשתף את המספר.")


def tenant_dirs(system: TradingSystem) -> dict:
    return {client_id: system.tenant(client_id).data_dir for client_id in system.registry.list_ids()}


def route(system: TradingSystem, bot: TelegramBot, update: dict) -> None:
    """One update from any chat: a link, a button press or a question, or a polite no."""
    chat = chat_of(update)
    if not chat:
        return
    dirs = tenant_dirs(system)
    here = bot.for_chat(chat)
    message = update.get("message") or {}
    linked = phone_link.claim(dirs, message, datetime.now(timezone.utc)) if message else None
    if linked:
        here.send(LINKED.format(client=linked))
        return
    client_id = phone_link.client_for_chat(dirs, chat)
    operator = chat == bot.chat_id
    if not operator and client_id is None:
        if str(message.get("text") or "").startswith("/start") or message.get("contact"):
            here.ask_phone(STRANGER)
        return
    # The operator's chat speaks for the platform even if it is also linked to a client.
    owner = None if operator else client_id
    text = question(update, chat)
    if text is not None:
        ask_expert(system, here, text, owner)
        return
    press(here, update, lambda data: answer_button(system, data, owner))


def watch(
    system: TradingSystem,
    book: AlertBook,
    notifier: Notifier,
    outcomes: dict[str, str] | None = None,
    *,
    morning: bool = False,
) -> None:
    """Every due tenant: alerts, a message per buy and sale, and the morning question."""
    now = datetime.now(timezone.utc)
    names = asset_names(system.settings)
    for client_id in due_tenants(system):
        session = None
        try:
            session = system.for_client(client_id)
            alerts = inspect(session)
            failure = cycle_failure(client_id, (outcomes or {}).get(client_id, ""))
            if failure is not None:
                alerts.append(failure)
            for message in book.update(client_id, alerts, now):
                notifier.send(message)
            # Trades and the morning question go to the client's own chat once linked;
            # until then, as before, to the operator's.
            chat = phone_link.linked_chat(session.tenant.data_dir)
            client = notifier.for_chat(chat) if chat and isinstance(notifier, TelegramBot) else notifier
            for message in new_messages(session.journal, session.tenant.data_dir, now, names):
                client.send(message)
            if morning:
                ask_morning(session, client, names)
        except Exception:
            # The watchdog must never take the trading loop down with it.
            log.exception("watchdog failed for %s", client_id)
        finally:
            if session is not None:
                session.close()


def seconds_until(moment: datetime, now: datetime) -> float:
    return max(0.0, (moment - now).total_seconds())


def research_overnight(system: TradingSystem, notifier: Notifier) -> None:
    """The researcher's brief for the coming trading day, once, before the scout's round."""
    from tradesys.orchestration import market_brain, market_research

    settings = system.settings
    day = approval_day(datetime.now(timezone.utc))
    if not market_research.research_due(settings.data_dir, day, market_brain.configured()):
        return
    market_research.count_attempt(settings.data_dir, day)
    try:
        brief = market_research.run_research(settings, day)
    except Exception:
        log.exception("market researcher failed; retrying on the next idle check")
        return
    if brief is None:
        return
    text = market_research.message(brief, day)
    chats = {phone_link.linked_chat(d) for d in tenant_dirs(system).values()} - {None}
    for part in market_brain.split_message(text):
        notifier.send(part)
        if isinstance(notifier, TelegramBot):
            for chat in chats - {notifier.chat_id}:
                notifier.for_chat(chat).send(part)


def scout_overnight(settings: Settings) -> None:
    """Write the talent scout's report for the coming trading day, once."""
    day = approval_day(datetime.now(timezone.utc))
    if not scout_due(settings, day):
        return
    try:
        scout_market(settings, day)
    except Exception:
        log.exception("talent scout failed; retrying on the next idle check")


def run_forever(
    system: TradingSystem,
    clock: MarketClock,
    cycle_minutes: float,
    notifier: Notifier | None = None,
) -> None:
    log.info("scheduler started: cycle every %.0f min while the market is open", cycle_minutes)
    book = AlertBook(system.settings.data_dir / "_platform" / "alerts.json")
    notifier = notifier or notifier_from_env()
    try:
        greet_once(notifier, system.settings.data_dir / "_platform" / "telegram_greeted.json")
    except Exception:  # a phone channel being down must never stop trading
        log.exception("could not send the alerts greeting")
    if isinstance(notifier, TelegramBot):
        threading.Thread(
            target=poll_forever,
            args=(notifier, lambda update: route(system, notifier, update), lambda: False),
            name="telegram-buttons",
            daemon=True,
        ).start()
    while True:
        try:
            status = clock.status()
            if status.is_open:
                watch(system, book, notifier, run_once(system))
                time.sleep(cycle_minutes * 60)
                continue
            wait = seconds_until(status.next_open, datetime.now(timezone.utc))
            watch(system, book, notifier, morning=wait <= MORNING_WINDOW_SECONDS)
            research_overnight(system, notifier)
            scout_overnight(system.settings)
            log.info(
                "market closed (%s); next open %s; sleeping %.0f min",
                status.source,
                status.next_open.isoformat(),
                min(wait, IDLE_RECHECK_SECONDS) / 60,
            )
            time.sleep(max(60.0, min(wait, IDLE_RECHECK_SECONDS)))
        except Exception:
            log.exception("scheduler loop error; retrying in 60s")
            time.sleep(60)


def _secret_resolver(settings: Settings):
    """Same resolution as the dashboard: encrypted vault first, then env vars.

    The vault module lives at the repo root next to app.py; when the scheduler
    runs from there it is importable, otherwise env-var credentials still work.
    """
    try:
        import accounts
    except ImportError:
        return None
    return accounts.VaultSecretResolver(settings.data_dir)


def main() -> None:
    settings = Settings.from_env()
    configure(settings.log_level)
    system = TradingSystem(settings, secrets=_secret_resolver(settings))
    clock = market_clock(settings.market_data_key_id, settings.market_data_secret_key)
    run_forever(system, clock, float(os.getenv("TRADESYS_CYCLE_MINUTES", "15")))


if __name__ == "__main__":
    main()

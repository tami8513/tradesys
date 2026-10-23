"""The Telegram channel: messages to the client's phone, and answers back from its buttons.

Only the configured chat is listened to. An update from any other chat is
dropped without a reply: a stranger who finds the bot must not be able to
press a button that sells a position.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("tradesys.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
Buttons = list[list[tuple[str, str]]]     # rows of (label, callback data)


class TelegramBot:
    def __init__(self, token: str, chat_id: str, *, http: Any = None) -> None:
        import httpx

        self._token = token
        self.chat_id = str(chat_id)
        # Identifies the channel without storing the token: a new token or chat greets again.
        self.fingerprint = hashlib.sha256(f"{token}|{chat_id}".encode()).hexdigest()[:16]
        self._http = http or httpx

    def _call(self, method: str, payload: dict, timeout: float = 15) -> Any:
        response = self._http.post(API.format(token=self._token, method=method), json=payload,
                                   timeout=timeout)
        response.raise_for_status()
        return response.json().get("result")

    def send(self, text: str, buttons: Buttons | None = None) -> int | None:
        """Send a message; failures are logged, never raised into the trading loop."""
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row] for row in buttons
            ]}
        try:
            result = self._call("sendMessage", payload)
            return int(result["message_id"]) if result else None
        except Exception:  # a phone channel being down must never stop trading
            log.exception("telegram send failed")
            return None

    def check(self) -> tuple[bool, str]:
        """Send a test message and say in plain Hebrew what is wrong, if anything."""
        try:
            me = self._http.post(API.format(token=self._token, method="getMe"), json={}, timeout=15)
            if me.status_code == 401:
                return False, ("הטוקן לא תקין או שבוטל. ב-BotFather שלח /revoke, בחר את הבוט, והעתק "
                               "ל-Render את הטוקן החדש בדיוק כמו שהוא.")
            name = (me.json().get("result") or {}).get("username", "")
            sent = self._http.post(
                API.format(token=self._token, method="sendMessage"),
                json={"chat_id": self.chat_id, "text": "🧪 הודעת בדיקה מ-tradesys: הטלפון מחובר."},
                timeout=15,
            )
        except Exception as exc:  # the network itself: tell the operator, do not crash the page
            return False, f"אין חיבור לטלגרם מהשרת ({type(exc).__name__})."
        if sent.status_code == 200:
            return True, f"נשלחה הודעת בדיקה מהבוט @{name}. בדוק את הטלפון."
        reason = str(sent.json().get("description", "")).lower()
        if sent.status_code == 403 or "initiate" in reason or "blocked" in reason:
            return False, (f"הבוט @{name} לא יכול לשלוח לך עדיין. פתח אותו בטלגרם, לחץ Start "
                           "ושלח לו הודעה כלשהי, ואז לחץ שוב על הבדיקה.")
        if "chat not found" in reason:
            return False, ("מספר הצ'אט לא נכון. ב-@userinfobot בדוק את המספר שליד Id, ועדכן ב-Render את "
                           "TRADESYS_TELEGRAM__CHAT_ID.")
        return False, f"טלגרם דחה את ההודעה: {sent.json().get('description', sent.status_code)}"

    def updates(self, offset: int, timeout: int = 25) -> list[dict]:
        return self._call("getUpdates", {"offset": offset, "timeout": timeout,
                                         "allowed_updates": ["callback_query", "message"]},
                          timeout=timeout + 10) or []

    def answer(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def edit(self, message_id: int, text: str) -> None:
        self._call("editMessageText", {"chat_id": self.chat_id, "message_id": message_id, "text": text})


def handle(update: dict, chat_id: str, on_choice: Callable[[str], str | None]) -> dict | None:
    """Turn one button press into what to tell the client, or None to ignore it.

    `on_choice(data)` applies the choice and returns the confirmation text, or
    None when the button is no longer valid (answered already, or expired).
    """
    query = update.get("callback_query")
    if not query:
        return None
    message = query.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != str(chat_id):
        return None
    confirmation = on_choice(str(query.get("data", "")))
    return {
        "callback_id": query["id"],
        "message_id": message.get("message_id"),
        "toast": confirmation or "השאלה הזו כבר לא בתוקף",
        "text": f"{message.get('text', '')}\n\n➡️ {confirmation}" if confirmation else None,
    }


def question(update: dict, chat_id: str) -> str | None:
    """The text the client wrote to the bot, or None for anything else or anyone else."""
    message = update.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != str(chat_id):
        return None
    text = message.get("text")
    return text.strip() if isinstance(text, str) and text.strip() else None


def poll_forever(
    bot: TelegramBot,
    on_choice: Callable[[str], str | None],
    stop: Callable[[], bool],
    on_text: Callable[[str], None] | None = None,
) -> None:
    """Long-poll for button presses and questions; runs on its own thread in the scheduler.

    A question can take a minute of research, so each one runs on a thread of
    its own and the buttons stay responsive meanwhile.
    """
    offset = 0
    while not stop():
        try:
            for update in bot.updates(offset):
                offset = max(offset, int(update["update_id"]) + 1)
                text = question(update, bot.chat_id)
                if text is not None and on_text is not None:
                    threading.Thread(target=on_text, args=(text,), name="telegram-question",
                                     daemon=True).start()
                    continue
                reply = handle(update, bot.chat_id, on_choice)
                if reply is None:
                    continue
                bot.answer(reply["callback_id"], reply["toast"])
                if reply["text"] and reply["message_id"]:
                    bot.edit(reply["message_id"], reply["text"])
        except Exception:  # keep listening through network hiccups
            log.exception("telegram polling failed")
            time.sleep(5)

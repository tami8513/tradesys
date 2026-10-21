"""The Telegram channel: messages to the client's phone, and answers back from its buttons.

Only the configured chat is listened to. An update from any other chat is
dropped without a reply: a stranger who finds the bot must not be able to
press a button that sells a position.
"""

from __future__ import annotations

import logging
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

    def updates(self, offset: int, timeout: int = 25) -> list[dict]:
        return self._call("getUpdates", {"offset": offset, "timeout": timeout,
                                         "allowed_updates": ["callback_query"]},
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


def poll_forever(bot: TelegramBot, on_choice: Callable[[str], str | None], stop: Callable[[], bool]) -> None:
    """Long-poll for button presses; runs on its own thread in the scheduler."""
    offset = 0
    while not stop():
        try:
            for update in bot.updates(offset):
                offset = max(offset, int(update["update_id"]) + 1)
                reply = handle(update, bot.chat_id, on_choice)
                if reply is None:
                    continue
                bot.answer(reply["callback_id"], reply["toast"])
                if reply["text"] and reply["message_id"]:
                    bot.edit(reply["message_id"], reply["text"])
        except Exception:  # keep listening through network hiccups
            log.exception("telegram polling failed")
            time.sleep(5)

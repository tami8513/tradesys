"""Linking a client's own Telegram to their account, from the portal.

The client asks for a link while logged in to the portal, with the phone number
on their Telegram. The portal gives a one-time code inside a t.me link; pressing
Start in the bot sends the code back and the chat is linked. A client who opens
the bot some other way shares their phone number from the bot instead, and it is
matched against the number they gave in the portal.

Both ways start from a logged-in portal session, so a stranger who finds the
bot can link nothing: an unknown code, a phone nobody asked to link, or a
contact card that is not the sender's own are all refused.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from tradesys.core.fileio import write_json_atomic

LINK_FILE = "telegram.json"
CODE_LIFETIME = timedelta(minutes=30)


def normalize_phone(raw: str) -> str:
    """Digits in international form; an Israeli 05x number becomes 9725x."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "972" + digits[1:]
    return digits


def read_link(tenant_dir: Path) -> dict:
    try:
        return json.loads((Path(tenant_dir) / LINK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def linked_chat(tenant_dir: Path) -> str | None:
    return read_link(tenant_dir).get("chat_id") or None


def start_link(tenant_dir: Path, phone: str, now: datetime) -> str:
    """A fresh one-time code for the client's link; any earlier link is replaced when it is used."""
    phone = normalize_phone(phone)
    if len(phone) < 9:
        raise ValueError("מספר טלפון לא תקין")
    code = secrets.token_urlsafe(9)
    write_json_atomic(Path(tenant_dir) / LINK_FILE, {
        **read_link(tenant_dir),
        "phone": phone,
        "code": code,
        "expires": (now + CODE_LIFETIME).isoformat(),
    })
    return code


def unlink(tenant_dir: Path) -> None:
    (Path(tenant_dir) / LINK_FILE).unlink(missing_ok=True)


def link_url(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={code}"


def _pending(link: dict, now: datetime) -> bool:
    try:
        return bool(link.get("code")) and now < datetime.fromisoformat(link["expires"])
    except (KeyError, ValueError):
        return False


def claim(tenant_dirs: dict[str, Path], message: dict, now: datetime) -> str | None:
    """Link the chat a message came from, if it carries a valid code or the sender's own phone.

    Returns the client id that was linked, or None.
    """
    chat = str((message.get("chat") or {}).get("id", ""))
    if not chat:
        return None
    text = str(message.get("text") or "")
    code = text.split(maxsplit=1)[1].strip() if text.startswith("/start ") else ""
    contact = message.get("contact") or {}
    own = contact.get("user_id") is not None and contact.get("user_id") == (message.get("from") or {}).get("id")
    phone = normalize_phone(contact.get("phone_number", "")) if own else ""
    if not code and not phone:
        return None
    for client_id, tenant_dir in tenant_dirs.items():
        link = read_link(tenant_dir)
        if not _pending(link, now):
            continue
        if (code and secrets.compare_digest(code, link["code"])) or (phone and phone == link.get("phone")):
            write_json_atomic(Path(tenant_dir) / LINK_FILE, {
                "chat_id": chat, "phone": link.get("phone", ""), "linked_at": now.isoformat(),
            })
            return client_id
    return None


def client_for_chat(tenant_dirs: dict[str, Path], chat: str) -> str | None:
    for client_id, tenant_dir in tenant_dirs.items():
        if linked_chat(tenant_dir) == str(chat):
            return client_id
    return None

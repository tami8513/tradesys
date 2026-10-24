from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tradesys import scheduler
from tradesys.orchestration import phone_link

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def dirs(tmp_path):
    found = {name: tmp_path / name for name in ("tal_dahan", "other")}
    for path in found.values():
        path.mkdir()
    return found


def start(code: str, chat: int = 555) -> dict:
    return {"chat": {"id": chat}, "from": {"id": chat}, "text": f"/start {code}"}


def contact(phone: str, owner: int, sender: int = 555) -> dict:
    return {"chat": {"id": sender}, "from": {"id": sender},
            "contact": {"phone_number": phone, "user_id": owner}}


def test_phone_numbers_compare_in_international_form():
    assert phone_link.normalize_phone("050-123-4567") == "972501234567"
    assert phone_link.normalize_phone("+972 50 123 4567") == "972501234567"
    assert phone_link.normalize_phone("00972501234567") == "972501234567"


def test_the_portal_code_links_the_chat_once(dirs):
    code = phone_link.start_link(dirs["tal_dahan"], "0501234567", NOW)

    assert phone_link.claim(dirs, start("wrong"), NOW) is None
    assert phone_link.claim(dirs, start(code), NOW) == "tal_dahan"
    assert phone_link.linked_chat(dirs["tal_dahan"]) == "555"
    assert phone_link.claim(dirs, start(code, chat=666), NOW) is None
    assert phone_link.client_for_chat(dirs, "555") == "tal_dahan"


def test_an_expired_code_links_nothing(dirs):
    code = phone_link.start_link(dirs["tal_dahan"], "0501234567", NOW)

    assert phone_link.claim(dirs, start(code), NOW + timedelta(hours=1)) is None


def test_only_the_senders_own_phone_links_and_only_when_asked_for(dirs):
    assert phone_link.claim(dirs, contact("+972501234567", owner=555), NOW) is None   # nobody asked
    phone_link.start_link(dirs["tal_dahan"], "050-1234567", NOW)

    assert phone_link.claim(dirs, contact("+972501234567", owner=777), NOW) is None   # someone else's card
    assert phone_link.claim(dirs, contact("+972501234567", owner=555), NOW) == "tal_dahan"


def test_a_bad_phone_is_refused(dirs):
    with pytest.raises(ValueError):
        phone_link.start_link(dirs["tal_dahan"], "12", NOW)


class Bot:
    def __init__(self, chat_id="1", log=None):
        self.chat_id, self.log = chat_id, log if log is not None else []

    def for_chat(self, chat):
        return Bot(chat, self.log)

    def send(self, text, buttons=None):
        self.log.append(("send", self.chat_id, text))

    def ask_phone(self, text):
        self.log.append(("ask_phone", self.chat_id, text))

    def answer(self, callback_id, text):
        self.log.append(("toast", self.chat_id, text))

    def edit(self, message_id, text):
        self.log.append(("edit", self.chat_id, text))


@pytest.fixture
def system(dirs, monkeypatch):
    asked = []
    monkeypatch.setattr(scheduler, "ask_expert", lambda system, bot, text, client_id=None:
                        asked.append((bot.chat_id, text, client_id)))
    return SimpleNamespace(registry=SimpleNamespace(list_ids=lambda: list(dirs)),
                           tenant=lambda client_id: SimpleNamespace(data_dir=dirs[client_id]),
                           asked=asked)


def button(chat: int, data: str) -> dict:
    return {"callback_query": {"id": "c1", "data": data,
                               "message": {"chat": {"id": chat}, "message_id": 9, "text": "?"}}}


def test_a_stranger_is_only_told_how_to_link(system):
    bot = Bot()

    scheduler.route(system, bot, {"message": {"chat": {"id": 999}, "text": "sell everything"}})
    scheduler.route(system, bot, button(999, "m|tal_dahan|q1|sell"))
    assert bot.log == [] and system.asked == []

    scheduler.route(system, bot, {"message": {"chat": {"id": 999}, "text": "/start"}})
    assert bot.log[0][:2] == ("ask_phone", "999")


def test_a_linked_client_asks_as_themselves_and_presses_only_their_own_buttons(system, dirs):
    bot = Bot()
    code = phone_link.start_link(dirs["tal_dahan"], "0501234567", datetime.now(timezone.utc))
    scheduler.route(system, bot, {"message": start(code)})
    assert bot.log[-1][:2] == ("send", "555")

    scheduler.route(system, bot, {"message": {"chat": {"id": 555}, "text": "מה בתיק שלי?"}})
    scheduler.route(system, bot, {"message": {"chat": {"id": 1}, "text": "מה בכל החשבונות?"}})
    assert system.asked == [("555", "מה בתיק שלי?", "tal_dahan"), ("1", "מה בכל החשבונות?", None)]

    scheduler.route(system, bot, button(555, "m|other|q1|sell"))
    assert bot.log[-1] == ("toast", "555", "השאלה הזו כבר לא בתוקף")

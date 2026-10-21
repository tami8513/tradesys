from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from test_position_monitor import Stack

from tradesys.orchestration import morning
from tradesys.orchestration.telegram import TelegramBot, handle
from tradesys.orchestration.trade_feed import new_messages

NAMES = {"RIVN": "Rivian"}.get


# ------------------------------------------------------------------ telegram

def press(chat_id, data="m|tal_dahan|q1|take"):
    return {"update_id": 7, "callback_query": {
        "id": "cb1", "data": data,
        "message": {"message_id": 42, "text": "☀️ RIVN", "chat": {"id": chat_id}},
    }}


def test_only_the_clients_own_chat_can_press_a_button():
    pressed = []
    assert handle(press(999), "123", pressed.append) is None
    assert pressed == []

    reply = handle(press(123), "123", lambda data: "RIVN: נמכר בפתיחה")
    assert reply["toast"] == "RIVN: נמכר בפתיחה"
    assert reply["text"].endswith("➡️ RIVN: נמכר בפתיחה") and reply["message_id"] == 42

    stale = handle(press(123), "123", lambda data: None)
    assert stale["toast"] == "השאלה הזו כבר לא בתוקף" and stale["text"] is None


def test_buttons_reach_telegram_and_a_failure_never_raises():
    sent = []

    class Http:
        def post(self, url, json, timeout):
            sent.append(json)
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"result": {"message_id": 5}})

    bot = TelegramBot("token", "123", http=Http())
    assert bot.send("hi", [[("🚀", "m|a|b|run")]]) == 5
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0] == {"text": "🚀", "callback_data": "m|a|b|run"}

    class Down:
        def post(self, *args, **kwargs):
            raise ConnectionError("no network")

    assert TelegramBot("token", "123", http=Down()).send("hi") is None


# ---------------------------------------------------------------- trade feed

def test_every_buy_and_sale_becomes_one_message(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    data_dir = stack.tenant.data_dir
    start = clock.now() - timedelta(minutes=1)
    assert new_messages(stack.journal, data_dir, start, NAMES) == []    # first run: no backlog

    trade_id = stack.open_position()
    [bought] = new_messages(stack.journal, data_dir, clock.now() + timedelta(seconds=1), NAMES)
    assert bought.startswith("🟢 קנייה · RIVN · Rivian")
    assert "🛡 הגנה: $18.00" in bought and "🎯 יעד" in bought

    stack.market.price = 17.5
    clock.advance(300)
    stack.monitor.check_once()
    [sold] = new_messages(stack.journal, data_dir, clock.now() + timedelta(seconds=1), NAMES)
    assert sold.startswith("🔻 מכירה בהפסד · RIVN")
    assert "ההגנה הופעלה ועצרה את ההפסד" in sold
    assert stack.journal.get_trade(trade_id).closed_at is not None

    assert new_messages(stack.journal, data_dir, clock.now() + timedelta(seconds=2), NAMES) == []


# ------------------------------------------------------------- morning ask

def test_only_a_position_well_in_profit_or_near_target_is_asked_about(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=20.0)
    trade = stack.journal.get_trade(stack.open_position())     # entry 20, stop 18, target 26

    assert not morning.worth_asking(trade, 21.0)                # +0.5R
    assert morning.worth_asking(trade, 23.0)                    # +1.5R
    assert not morning.worth_asking(trade, 19.0)


def test_an_answer_is_carried_out_at_the_open(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=23.0)
    trade_id = stack.open_position()
    session = SimpleNamespace(tenant=stack.tenant, journal=stack.journal, monitor=stack.monitor)
    data_dir = stack.tenant.data_dir

    [question] = morning.ask(data_dir, date(2026, 10, 22), stack.journal.open_trades(), {"RIVN": 23.0}, NAMES)
    assert "☀️ RIVN · Rivian" in question["text"]
    assert morning.answer(data_dir, question["id"], "nonsense") is None
    assert morning.answer(data_dir, question["id"], "take").startswith("RIVN: בפתיחת המסחר")

    events = morning.carry_out(session)

    assert ("client", "take_profit", "RIVN", "") in events
    assert stack.journal.get_trade(trade_id).closed_at is not None
    assert morning.pending(data_dir) == []
    assert morning.answer(data_dir, question["id"], "run") is None     # settled


def test_letting_it_run_removes_the_target_and_keeps_the_stop(tmp_path, clock):
    stack = Stack(tmp_path, clock, price=23.0)
    trade_id = stack.open_position()
    session = SimpleNamespace(tenant=stack.tenant, journal=stack.journal, monitor=stack.monitor)
    data_dir = stack.tenant.data_dir
    [question] = morning.ask(data_dir, date(2026, 10, 22), stack.journal.open_trades(), {"RIVN": 23.0})
    morning.answer(data_dir, question["id"], "run")

    morning.carry_out(session)

    trade = stack.journal.get_trade(trade_id)
    assert trade.target_price is None and trade.stop_price == 18.0 and trade.closed_at is None


def test_a_button_press_reaches_the_right_clients_question(tmp_path, clock):
    from pathlib import Path

    from tradesys.core.config import Settings
    from tradesys.orchestration.engine import TradingSystem
    from tradesys.scheduler import answer_button

    root = Path(__file__).resolve().parent.parent
    system = TradingSystem(Settings(clients_dir=root / "clients", data_dir=tmp_path))
    stack = Stack(tmp_path / "stack", clock, price=23.0)
    stack.open_position()
    data_dir = system.tenant("tal_dahan").data_dir
    [question] = morning.ask(data_dir, date(2026, 10, 22), stack.journal.open_trades(), {"RIVN": 23.0})

    assert answer_button(system, f"m|tal_dahan|{question['id']}|run").startswith("RIVN: היעד הוסר")
    assert answer_button(system, f"m|stranger|{question['id']}|run") is None
    assert answer_button(system, "garbage") is None

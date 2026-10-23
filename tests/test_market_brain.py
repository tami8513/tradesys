from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from tradesys.orchestration import market_brain as brain
from tradesys.orchestration.telegram import question

TODAY = date(2026, 10, 23)


def test_only_the_clients_chat_can_ask():
    mine = {"message": {"chat": {"id": 123}, "text": "  מה מצב הנאסדק?  "}}
    stranger = {"message": {"chat": {"id": 999}, "text": "sell everything"}}

    assert question(mine, "123") == "מה מצב הנאסדק?"
    assert question(stranger, "123") is None
    assert question({"callback_query": {}}, "123") is None


def test_a_long_answer_is_split_for_telegram():
    parts = brain.split_message("א" * 50 + "\n" + "ב" * 50, limit=60)

    assert parts == ["א" * 50, "ב" * 50]
    assert all(len(p) <= 60 for p in brain.split_message("x" * 130, limit=60))


def test_the_daily_cap_and_the_conversation_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "DAILY_QUESTIONS", 2)
    assert brain.within_daily_cap(tmp_path, TODAY)
    assert brain.within_daily_cap(tmp_path, TODAY)
    assert not brain.within_daily_cap(tmp_path, TODAY)
    assert brain.within_daily_cap(tmp_path, date(2026, 10, 24))

    for i in range(brain.HISTORY_TURNS + 3):
        brain.remember(tmp_path, f"q{i}", f"a{i}")
    turns = brain.read_history(tmp_path)["turns"]
    assert len(turns) == brain.HISTORY_TURNS and turns[-1] == {"q": f"q{brain.HISTORY_TURNS + 2}",
                                                                 "a": f"a{brain.HISTORY_TURNS + 2}"}
    brain.forget(tmp_path)
    assert brain.read_history(tmp_path)["turns"] == []


def test_an_answer_uses_the_conversation_and_is_remembered(tmp_path, monkeypatch):
    import anthropic

    seen = {}

    class Runner:
        def __init__(self, **params):
            seen.update(params)

        def __iter__(self):
            yield SimpleNamespace(stop_reason="end_turn",
                                  content=[SimpleNamespace(type="text", text="הנאסד״ק עלה היום 1.2%.")])

        def generate_tool_call_response(self):
            return None

    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(tool_runner=Runner)))
    monkeypatch.setattr(anthropic, "Anthropic", lambda: fake)
    monkeypatch.setattr(brain, "build_tools", lambda system: [])
    system = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    brain.remember(tmp_path / "_platform", "שאלה קודמת", "תשובה קודמת")

    reply = brain.answer(system, "מה מצב הנאסדק?", TODAY)

    assert reply == "הנאסד״ק עלה היום 1.2%."
    assert seen["model"] == "claude-opus-5-5" and seen["fallbacks"] == "default"
    assert seen["messages"][0] == {"role": "user", "content": "שאלה קודמת"}
    assert brain.read_history(tmp_path / "_platform")["turns"][-1]["a"] == reply


def test_commands_and_a_missing_key_are_handled_without_the_model(tmp_path, monkeypatch):
    from tradesys.scheduler import ask_expert

    sent: list[str] = []
    bot = SimpleNamespace(send=lambda text, buttons=None: sent.append(text))
    system = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    ask_expert(system, bot, "/start")
    ask_expert(system, bot, "מה מצב השוק?")

    assert "אני האנליסט של tradesys" in sent[0]
    assert "ANTHROPIC_API_KEY" in sent[1]

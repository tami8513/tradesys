from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from tradesys.orchestration import market_research as research
from tradesys.orchestration.track_record import research_calls

DAY = date(2026, 10, 26)
KNOWN = {"LMT", "RTX", "NOC", "ITA", "XLE", "XOM", "CVX", "BA"}

RAW = {
    "headline": "מתיחות במזרח התיכון מקפיצה את הנפט",
    "market_view": "השוק נזהר; הנפט עלה 4%.",
    "indices": [
        {"symbol": "SPY", "stance": "neutral", "why": "מחכה לפד"},
        {"symbol": "XYZ", "stance": "positive", "why": "לא מדד"},
        {"symbol": "QQQ", "stance": "bullish", "why": "עמדה לא חוקית"},
    ],
    "themes": [
        {"name": "ביטחון", "why": "תקציבי ביטחון עולים", "horizon": "months", "confidence": 9,
         "early": False, "funds": ["ita"], "stocks": ["LMT", "rtx", "FAKE1", "NOC", "LMT"], "risks": "הפסקת אש"},
        {"name": "נושא בלי מניות אמיתיות", "why": "", "horizon": "soon", "confidence": 3,
         "early": True, "funds": [], "stocks": ["NOPE"], "risks": ""},
        {"name": "נפט", "why": "היצע נפגע", "horizon": "weeks", "confidence": "x",
         "early": True, "funds": ["XLE"], "stocks": ["XOM", "CVX"], "risks": "הסכם"},
    ],
    "avoid": [{"symbol": "BA", "why": "עלויות דלק"}, {"symbol": "??", "why": "x"}],
}


def test_the_brief_keeps_only_real_tickers_and_valid_fields():
    brief = research.clean(RAW, KNOWN.__contains__)

    assert [i["symbol"] for i in brief["indices"]] == ["SPY"]
    assert [t["name"] for t in brief["themes"]] == ["ביטחון", "נפט"]
    defense, oil = brief["themes"]
    assert defense["stocks"] == ["LMT", "RTX", "NOC"] and defense["funds"] == ["ITA"]
    assert defense["confidence"] == 5 and oil["confidence"] == 3
    assert brief["avoid"] == [{"symbol": "BA", "why": "עלויות דלק"}]
    assert research.theme_symbols(brief) == {"LMT": "ביטחון", "RTX": "ביטחון", "NOC": "ביטחון",
                                             "ITA": "ביטחון", "XOM": "נפט", "CVX": "נפט", "XLE": "נפט"}


def test_the_message_says_it_is_research_not_advice():
    text = research.message(research.clean(RAW, KNOWN.__contains__), DAY)

    assert text.startswith("📰 חוקר השוק · 26/10")
    assert "מניות: LMT, RTX, NOC" in text and "מה יפריך: הפסקת אש" in text
    assert "זה מחקר, לא המלצת קנייה" in text


def test_a_paused_search_is_resumed_until_the_brief_is_submitted(monkeypatch):
    import anthropic

    replies = [
        SimpleNamespace(stop_reason="pause_turn", content=[SimpleNamespace(type="text", text="מחפש…")]),
        SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="tool_use", name="submit_brief", input={"headline": "x"})]),
    ]
    calls = []

    def create(**params):
        calls.append(params)
        return replies[len(calls) - 1]

    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(anthropic, "Anthropic", lambda: fake)

    assert research.ask_model("brief") == {"headline": "x"}
    assert len(calls) == 2 and calls[0]["output_config"] == {"effort": "high"}


def test_the_researchers_stocks_are_graded_at_the_price_they_were_named(tmp_path):
    platform = tmp_path / "_platform"
    platform.mkdir()
    brief = research.clean(RAW, KNOWN.__contains__)
    brief.update(made_at="2026-10-23T21:00:00+00:00", prices={"LMT": 600.0, "XOM": 120.0})
    (platform / "research_2026-10-26.json").write_text(json.dumps(brief), encoding="utf-8")

    calls = research_calls(platform, DAY)

    assert [(c.symbol, c.label, c.price) for c in calls] == [("LMT", "R", 600.0), ("XOM", "R", 120.0)]
    assert research.latest_brief(tmp_path, DAY)["headline"] == RAW["headline"]


def test_the_brief_goes_to_the_operator_and_every_linked_client(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from tradesys import scheduler
    from tradesys.orchestration import market_brain, phone_link

    dirs = {"tal_dahan": tmp_path / "tal_dahan", "other": tmp_path / "other"}
    for path in dirs.values():
        path.mkdir()
    code = phone_link.start_link(dirs["other"], "0501234567", datetime.now(timezone.utc))
    phone_link.claim(dirs, {"chat": {"id": 77}, "from": {"id": 77}, "text": f"/start {code}"},
                     datetime.now(timezone.utc) + timedelta(seconds=1))

    sent = []

    class Bot(scheduler.TelegramBot):
        def __init__(self, chat_id):
            self.chat_id = chat_id

        def for_chat(self, chat):
            return Bot(chat)

        def send(self, text, buttons=None):
            sent.append((self.chat_id, text[:12]))

    monkeypatch.setattr(market_brain, "configured", lambda: True)
    monkeypatch.setattr(research, "run_research", lambda settings, day: research.clean(RAW, KNOWN.__contains__))
    system = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path),
                             registry=SimpleNamespace(list_ids=lambda: list(dirs)),
                             tenant=lambda client_id: SimpleNamespace(data_dir=dirs[client_id]))

    scheduler.research_overnight(system, Bot("1"))

    assert {chat for chat, _ in sent} == {"1", "77"}


def test_a_day_gets_two_tries_at_most(tmp_path):
    (tmp_path / "_platform").mkdir()
    assert research.research_due(tmp_path, DAY, True)
    research.count_attempt(tmp_path, DAY)
    research.count_attempt(tmp_path, DAY)
    assert not research.research_due(tmp_path, DAY, True)
    assert not research.research_due(tmp_path, date(2026, 10, 27), False)

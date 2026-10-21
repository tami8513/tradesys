from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradesys.core.models import Bar
from tradesys.data.index_board import (
    BoardSettings,
    clean,
    quotes_from_bars,
    read_board,
    write_board,
)


def test_a_new_client_sees_the_leading_indices_large():
    board = BoardSettings()

    assert board.featured == ("SPY", "QQQ", "DIA", "IWM")
    assert board.small("מדדים מובילים") == ["MDY"]


def test_the_choice_is_validated_and_keeps_the_clients_order():
    board = clean(
        featured=["QQQ", "EIS", "QQQ", "NOPE"],
        hidden=["XLU", "QQQ", "BAD"],
        groups=["בינלאומי", "לא קיים", "סקטורים"],
    )

    assert board.featured == ("QQQ", "EIS")
    assert board.hidden == ("XLU",)
    assert board.groups == ("בינלאומי", "סקטורים")
    assert "XLU" not in board.small("סקטורים")
    assert "EIS" not in board.small("בינלאומי")


def test_settings_round_trip_and_a_broken_file_falls_back(tmp_path):
    board = clean(["IWM", "SPY"], ["GLD"], ["קריפטו"])
    write_board(tmp_path, board)
    assert read_board(tmp_path) == board

    (tmp_path / "index_board.json").write_text("not json", encoding="utf-8")
    assert read_board(tmp_path) == BoardSettings()


def test_quotes_compare_the_last_close_with_the_one_before():
    start = datetime(2026, 10, 19, tzinfo=timezone.utc)
    bars = [Bar(start + timedelta(days=i), c, c, c, c, 1) for i, c in enumerate((100, 102))]

    quote = quotes_from_bars({"SPY": bars, "NEW": bars[:1]})

    assert quote["SPY"].price == 102 and round(quote["SPY"].change_pct, 6) == 2.0
    assert quote["SPY"].closes == [100, 102]
    assert "NEW" not in quote

from __future__ import annotations

from tradesys.data.asset_search import AssetDirectory

LISTING = [
    {"symbol": "NVDA", "name": "NVIDIA Corporation Common Stock", "exchange": "NASDAQ", "tradable": True},
    {"symbol": "NVDL", "name": "GraniteShares 2x Long NVDA Daily ETF", "exchange": "NASDAQ", "tradable": True},
    {"symbol": "SPY", "name": "State Street SPDR S&P 500 ETF Trust", "exchange": "ARCA", "tradable": True},
    {"symbol": "VOO", "name": "Vanguard S&P 500 ETF", "exchange": "ARCA", "tradable": True},
    {"symbol": "TEVA", "name": "Teva Pharmaceutical Industries Limited", "exchange": "NYSE", "tradable": True},
    {"symbol": "PNKX", "name": "Pink Sheet Example Inc", "exchange": "OTC", "tradable": True},
    {"symbol": "DEAD", "name": "Delisted Corp", "exchange": "NYSE", "tradable": False},
]


def directory(tmp_path, calls=None):
    def fetch():
        if calls is not None:
            calls.append(1)
        return LISTING

    return AssetDirectory(tmp_path, fetch)


def test_ticker_search_puts_the_exact_match_first(tmp_path):
    hits = directory(tmp_path).search("nvda")
    assert [h.symbol for h in hits][:2] == ["NVDA", "NVDL"]
    assert hits[0].kind == "מניה" and hits[1].kind == "קרן ממונפת (סיכון גבוה)"


def test_a_plain_fund_is_labelled_a_fund(tmp_path):
    assert directory(tmp_path).search("VOO")[0].kind == "קרן סל"


def test_name_search_finds_the_company(tmp_path):
    assert directory(tmp_path).search("teva")[0].symbol == "TEVA"


def test_hebrew_name_finds_the_israeli_company(tmp_path):
    assert directory(tmp_path).search("טבע")[0].symbol == "TEVA"


def test_an_index_returns_the_funds_that_track_it(tmp_path):
    hits = directory(tmp_path).search("S&P 500")
    assert [h.symbol for h in hits][:2] == ["SPY", "VOO"]
    assert "עוקבת אחרי" in hits[0].note


def test_otc_and_untradable_names_are_left_out(tmp_path):
    assert directory(tmp_path).search("PNKX") == []
    assert directory(tmp_path).search("DEAD") == []


def test_the_listing_is_downloaded_once_a_day(tmp_path):
    calls: list[int] = []
    directory(tmp_path, calls).search("nvda")
    directory(tmp_path, calls).search("spy")
    assert len(calls) == 1


def test_a_single_letter_does_not_drag_in_every_index(tmp_path):
    hits = directory(tmp_path).search("s")
    assert all(not h.note for h in hits)

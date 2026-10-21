"""Search every US stock and fund the broker can trade, by ticker or by name.

The directory comes from the broker's own asset list, so a result is something an
order can actually be placed in; OTC names are left out, being thin and hard to
exit. It is cached on disk for a day: listings change slowly, and a client typing
into a search box should not cost a 14,000-row download per keystroke.

Indices themselves cannot be bought, so a search for one returns the funds that
track it. Israeli clients can search well-known names in Hebrew.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

from tradesys.core.fileio import write_json_atomic

LISTED_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
_LEVERAGED_WORDS = ("2X", "3X", "-1X", "INVERSE", "ULTRA", " BEAR ", " BULL ", "LEVERAGED")
_FUND_WORDS = ("ETF", " TRUST", " FUND", "ISHARES", "SPDR", "INVESCO QQQ", "VANGUARD")

INDEX_FUNDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "s&p 500": ("מדד S&P 500, 500 החברות הגדולות בארה\"ב", ("SPY", "VOO", "IVV")),
    "sp500": ("מדד S&P 500, 500 החברות הגדולות בארה\"ב", ("SPY", "VOO", "IVV")),
    "אס אנד פי": ("מדד S&P 500, 500 החברות הגדולות בארה\"ב", ("SPY", "VOO", "IVV")),
    "nasdaq": ("מדד נאסד\"ק 100, חברות הטכנולוגיה הגדולות", ("QQQ", "QQQM")),
    "נאסדק": ("מדד נאסד\"ק 100, חברות הטכנולוגיה הגדולות", ("QQQ", "QQQM")),
    "dow": ("מדד דאו ג'ונס, 30 חברות ותיקות", ("DIA",)),
    "דאו": ("מדד דאו ג'ונס, 30 חברות ותיקות", ("DIA",)),
    "russell": ("מדד ראסל 2000, חברות קטנות", ("IWM",)),
    "ראסל": ("מדד ראסל 2000, חברות קטנות", ("IWM",)),
    "gold": ("זהב", ("GLD", "IAU")),
    "זהב": ("זהב", ("GLD", "IAU")),
    "bonds": ("אג\"ח ממשלת ארה\"ב", ("TLT", "IEF", "SHY")),
    "אגח": ("אג\"ח ממשלת ארה\"ב", ("TLT", "IEF", "SHY")),
    "bitcoin": ("ביטקוין (דרך קרן סל)", ("IBIT", "FBTC")),
    "ביטקוין": ("ביטקוין (דרך קרן סל)", ("IBIT", "FBTC")),
    "oil": ("נפט ואנרגיה", ("USO", "XLE")),
    "נפט": ("נפט ואנרגיה", ("USO", "XLE")),
    "semiconductors": ("שבבים", ("SMH", "SOXX")),
    "שבבים": ("שבבים", ("SMH", "SOXX")),
}

HEBREW_NAMES: dict[str, str] = {
    "אפל": "AAPL", "מיקרוסופט": "MSFT", "אנבידיה": "NVDA", "אמזון": "AMZN",
    "גוגל": "GOOGL", "אלפבית": "GOOGL", "מטא": "META", "פייסבוק": "META",
    "טסלה": "TSLA", "נטפליקס": "NFLX", "פלנטיר": "PLTR", "אינטל": "INTC",
    "טבע": "TEVA", "צק פוינט": "CHKP", "צ'ק פוינט": "CHKP", "וויקס": "WIX",
    "מובילאיי": "MBLY", "נייס": "NICE", "מאנדיי": "MNDY", "סולאראדג": "SEDG",
    "אלביט": "ESLT", "קוקה קולה": "KO", "דיסני": "DIS", "בואינג": "BA",
    "וולמארט": "WMT", "ג'יי פי מורגן": "JPM", "שופיפיי": "SHOP", "אובר": "UBER",
}


@dataclass(frozen=True, slots=True)
class AssetHit:
    symbol: str
    name: str
    kind: str        # "מניה" | "קרן סל"
    note: str = ""   # why it matched, when it was not by ticker or name


def _kind(name: str) -> str:
    upper = name.upper()
    if any(word in upper for word in _LEVERAGED_WORDS):
        # Daily-reset 2x/3x and inverse funds decay when held, and move two to
        # three times the market; a client adding one should know it.
        return "קרן ממונפת (סיכון גבוה)"
    return "קרן סל" if any(word in upper for word in _FUND_WORDS) else "מניה"


class AssetDirectory:
    def __init__(self, cache_dir: Path, fetch: Callable[[], list[dict]]) -> None:
        self._path = Path(cache_dir) / f"assets_{date.today().isoformat()}.json"
        self._fetch = fetch
        self._assets: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._assets is None:
            if self._path.exists():
                self._assets = json.loads(self._path.read_text(encoding="utf-8"))
            else:
                self._assets = {
                    a["symbol"]: a["name"]
                    for a in self._fetch()
                    if a.get("tradable") and a.get("exchange") in LISTED_EXCHANGES
                }
                self._path.parent.mkdir(parents=True, exist_ok=True)
                write_json_atomic(self._path, self._assets)
        return self._assets

    def name(self, symbol: str) -> str | None:
        return self._load().get(symbol)

    def search(self, query: str, limit: int = 10) -> list[AssetHit]:
        text = query.strip()
        if not text:
            return []
        assets = self._load()
        hits: list[AssetHit] = []

        def add(symbol: str, note: str = "") -> None:
            if symbol in assets and all(h.symbol != symbol for h in hits):
                hits.append(AssetHit(symbol, assets[symbol], _kind(assets[symbol]), note))

        lowered = text.lower()
        partial = len(lowered) >= 3     # one or two letters would match half the aliases

        def matches(alias: str) -> bool:
            return alias == lowered or (partial and (alias in lowered or lowered in alias))

        for alias, (label, funds) in INDEX_FUNDS.items():
            if matches(alias):
                for symbol in funds:
                    add(symbol, f"עוקבת אחרי {label}")
        for hebrew, symbol in HEBREW_NAMES.items():
            if matches(hebrew):
                add(symbol)

        ticker = text.upper()
        add(ticker)
        ranked: Sequence[tuple[int, str]] = sorted(
            (rank, symbol)
            for symbol, name in assets.items()
            if (rank := _rank(ticker, symbol, name.upper())) is not None
        )
        for _, symbol in ranked:
            if len(hits) >= limit:
                break
            add(symbol)
        return hits[:limit]


def _rank(ticker: str, symbol: str, name: str) -> int | None:
    """Lower is better: ticker prefix, then a word of the name, then anywhere."""
    if symbol.startswith(ticker):
        return len(symbol)
    if len(ticker) >= 3 and any(word.startswith(ticker) for word in name.split()):
        return 10 + len(symbol)
    if len(ticker) >= 3 and ticker in name:
        return 20 + len(symbol)
    return None

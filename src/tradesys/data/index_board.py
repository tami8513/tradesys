"""The index board on the client's home page: which indices, how big, in what order.

Indices cannot be bought, so each one is shown through the fund that tracks it.
The client chooses which appear as large cards and in what order, which to hide,
and which groups to show; the choice is a display preference saved per tenant.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tradesys.core.fileio import write_json_atomic

BOARD_FILE = "index_board.json"
MAX_FEATURED = 6

GROUPS: dict[str, tuple[tuple[str, str], ...]] = {
    "מדדים מובילים": (
        ("SPY", "S&P 500"), ("QQQ", "נאסד\"ק 100"), ("DIA", "דאו ג'ונס"),
        ("IWM", "ראסל 2000"), ("MDY", "חברות בינוניות"),
    ),
    "סקטורים": (
        ("XLK", "טכנולוגיה"), ("SMH", "שבבים"), ("XLF", "פיננסים"), ("XLV", "בריאות"),
        ("XLE", "אנרגיה"), ("XLI", "תעשייה"), ("XLY", "צריכה"), ("XLC", "תקשורת"),
        ("XLU", "תשתיות"), ("XLRE", "נדל\"ן"),
    ),
    "סחורות ואג\"ח": (
        ("GLD", "זהב"), ("SLV", "כסף"), ("USO", "נפט"), ("TLT", "אג\"ח ארה\"ב ארוכות"),
    ),
    "בינלאומי": (
        ("EIS", "ישראל"), ("EFA", "אירופה ויפן"), ("EEM", "שווקים מתעוררים"),
        ("FXI", "סין"), ("EWJ", "יפן"),
    ),
    "קריפטו": (("IBIT", "ביטקוין"),),
}
NAMES = {symbol: name for members in GROUPS.values() for symbol, name in members}
GROUP_OF = {symbol: group for group, members in GROUPS.items() for symbol, _ in members}
DEFAULT_FEATURED = ("SPY", "QQQ", "DIA", "IWM")


@dataclass(frozen=True, slots=True)
class BoardSettings:
    featured: tuple[str, ...] = DEFAULT_FEATURED      # large cards, in this order
    hidden: tuple[str, ...] = ()
    groups: tuple[str, ...] = tuple(GROUPS)           # groups shown below, in this order

    def small(self, group: str) -> list[str]:
        """The group's remaining indices, shown as small cards."""
        return [
            symbol for symbol, _ in GROUPS.get(group, ())
            if symbol not in self.featured and symbol not in self.hidden
        ]


def clean(featured, hidden, groups) -> BoardSettings:
    """Validate a choice at the boundary: known symbols and groups only, no repeats."""
    featured = tuple(dict.fromkeys(s for s in featured if s in NAMES))[:MAX_FEATURED]
    hidden = tuple(dict.fromkeys(s for s in hidden if s in NAMES and s not in featured))
    groups = tuple(dict.fromkeys(g for g in groups if g in GROUPS))
    return BoardSettings(featured, hidden, groups)


def read_board(tenant_dir: Path) -> BoardSettings:
    path = Path(tenant_dir) / BOARD_FILE
    if not path.exists():
        return BoardSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return clean(raw.get("featured", ()), raw.get("hidden", ()), raw.get("groups", ()))
    except (ValueError, OSError, AttributeError, TypeError):
        return BoardSettings()


def write_board(tenant_dir: Path, settings: BoardSettings) -> None:
    write_json_atomic(Path(tenant_dir) / BOARD_FILE, asdict(settings))


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: float
    change_pct: float                                 # against the previous close
    closes: list[float] = field(default_factory=list)  # recent daily closes, for the sparkline


def quotes_from_bars(bars_by_symbol: dict[str, list]) -> dict[str, Quote]:
    quotes = {}
    for symbol, bars in bars_by_symbol.items():
        if len(bars) >= 2 and bars[-2].close:
            quotes[symbol] = Quote(
                symbol,
                bars[-1].close,
                (bars[-1].close / bars[-2].close - 1) * 100,
                [b.close for b in bars],
            )
    return quotes

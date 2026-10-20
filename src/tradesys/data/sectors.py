"""Which sector each approved name belongs to, for the diversification checks.

A static map: sector membership changes rarely, and a lookup that can fail would
make a risk limit depend on a vendor being up. Broad-market ETFs get their own
group, since they hold every sector at once. A name missing here is its own
sector of one, so the sector cap never blocks it; the correlation check still
catches it if it moves in lockstep with something already held.
"""

from __future__ import annotations

SECTOR_NAMES = {
    "technology": "טכנולוגיה",
    "communication": "תקשורת ומדיה",
    "consumer": "צריכה",
    "staples": "מוצרי צריכה בסיסיים",
    "financials": "פיננסים",
    "health": "בריאות",
    "energy": "אנרגיה",
    "industrials": "תעשייה",
    "materials": "חומרי גלם",
    "utilities": "תשתיות",
    "real_estate": "נדל\"ן",
    "broad": "מדד רחב",
    "bonds_gold": "אג\"ח וזהב",
}

_GROUPS: dict[str, tuple[str, ...]] = {
    "technology": (
        "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "AMD", "ADBE", "CRM", "INTC", "CSCO",
        "QCOM", "TXN", "AMAT", "MU", "NOW", "INTU", "PANW", "ANET", "SHOP", "PLTR",
        "CRWD", "MRVL", "XLK", "SMH",
    ),
    "communication": ("GOOGL", "META", "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS", "XLC"),
    "consumer": ("AMZN", "TSLA", "HD", "LOW", "MCD", "NKE", "SBUX", "TGT", "UBER", "XLY"),
    "staples": ("WMT", "COST", "PG", "KO", "PEP", "XLP"),
    "financials": (
        "JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "AXP", "BLK", "SCHW", "XLF",
    ),
    "health": (
        "UNH", "LLY", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "ISRG", "AMGN",
        "XLV", "XBI",
    ),
    "energy": ("XOM", "CVX", "COP", "SLB", "EOG", "XLE"),
    "industrials": (
        "CAT", "DE", "GE", "HON", "BA", "LMT", "RTX", "UPS", "UNP", "XLI",
    ),
    "materials": ("LIN", "FCX", "XLB"),
    "utilities": ("NEE", "DUK", "XLU"),
    "real_estate": ("XLRE",),
    "broad": ("SPY", "QQQ", "IWM", "DIA"),
    "bonds_gold": ("TLT", "GLD"),
}

SECTORS: dict[str, str] = {
    symbol: sector for sector, symbols in _GROUPS.items() for symbol in symbols
}


def sector_of(symbol: str) -> str:
    """The sector key, or the symbol itself for a name the map does not know."""
    return SECTORS.get(symbol.upper(), symbol.upper())


def sector_label(symbol: str) -> str:
    sector = sector_of(symbol)
    return SECTOR_NAMES.get(sector, sector)

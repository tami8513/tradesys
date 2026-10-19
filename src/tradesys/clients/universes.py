"""Named stock universes an operator can let a client's leader hunter choose from.

The universe is the boundary: the Risk Manager refuses any symbol outside it, so
the hunter can rotate freely without ever reaching a ticker nobody approved.

`leaders` is large, liquid US companies plus sector and broad ETFs, all listed
for the whole test window. Because it is today's list of large companies, a
backtest over it carries survivorship bias: names that shrank or vanished are
missing. Results on it are an upper bound, not a forecast.
"""

from __future__ import annotations

UNIVERSES: dict[str, tuple[str, ...]] = {
    "leaders": (
        # broad market and sectors
        "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY",
        "XLP", "XLU", "XLB", "XLRE", "XLC", "SMH", "XBI", "GLD", "TLT",
        # mega and large caps
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "ORCL",
        "AMD", "ADBE", "CRM", "NFLX", "INTC", "CSCO", "QCOM", "TXN", "AMAT", "MU",
        "NOW", "INTU", "PANW", "ANET", "UBER", "SHOP", "PLTR", "CRWD", "MRVL",
        "JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "AXP", "BLK", "SCHW",
        "UNH", "LLY", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "ISRG", "AMGN",
        "XOM", "CVX", "COP", "SLB", "EOG",
        "WMT", "COST", "HD", "LOW", "MCD", "NKE", "SBUX", "TGT", "PG", "KO", "PEP",
        "CAT", "DE", "GE", "HON", "BA", "LMT", "RTX", "UPS", "UNP",
        "DIS", "CMCSA", "T", "VZ", "TMUS", "NEE", "DUK", "LIN", "FCX",
    ),
}

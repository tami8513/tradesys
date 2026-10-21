"""The index robot's settings and its own book, kept apart from the main robot's.

The index robot holds index funds without stops, as a long-term core. The main
robot's protections (drawdown halt, daily loss limit, capital floor) are built
for short trades with stops, so they must not see the index holdings move: a
7% dip in the S&P 500 on a large index budget would otherwise read as a 2%
drawdown and halt the main robot. The ledger here records the money moved into
the index robot, and `book_equity` gives the main robot the account without
the index robot's profit or loss.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tradesys.core.fileio import write_json_atomic

SETTINGS_FILE = "index_robot.json"
LEDGER_FILE = "index_ledger.json"
INDEX_CHOICES = ("SPY", "QQQ", "DIA", "IWM", "MDY")
DEFAULT_INDICES = ("SPY", "QQQ")


@dataclass(frozen=True, slots=True)
class SleeveSettings:
    enabled: bool = False
    budget: float = 0.0                      # dollars the robot may put in, at cost
    indices: tuple[str, ...] = DEFAULT_INDICES
    bear_insurance: bool = False             # cut to half in a death cross
    acknowledged: bool = False               # the client accepted holding without a stop


def clean_settings(enabled, budget, indices, bear_insurance, acknowledged) -> SleeveSettings:
    """Validate a choice at the boundary."""
    chosen = tuple(dict.fromkeys(s for s in indices if s in INDEX_CHOICES))
    return SleeveSettings(
        enabled=bool(enabled) and bool(acknowledged) and bool(chosen),
        budget=max(0.0, float(budget)),
        indices=chosen,
        bear_insurance=bool(bear_insurance),
        acknowledged=bool(acknowledged),
    )


def read_settings(tenant_dir: Path) -> SleeveSettings:
    path = Path(tenant_dir) / SETTINGS_FILE
    if not path.exists():
        return SleeveSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return clean_settings(
            raw.get("enabled", False), raw.get("budget", 0.0), raw.get("indices", ()),
            raw.get("bear_insurance", False), raw.get("acknowledged", False),
        )
    except (ValueError, OSError, AttributeError, TypeError):
        return SleeveSettings()


def write_settings(tenant_dir: Path, settings: SleeveSettings) -> None:
    write_json_atomic(Path(tenant_dir) / SETTINGS_FILE, asdict(settings))


@dataclass(slots=True)
class Holding:
    qty: int = 0
    cost: float = 0.0                        # total dollars paid for the shares held


@dataclass(slots=True)
class Ledger:
    """The index robot's own accounts.

    `contributed` is money moved in from the main book (new budget), `parked`
    is the cash from an insurance sale waiting to go back into the index.
    The robot's profit is holdings + parked − contributed; the main robot
    never sees it.
    """

    holdings: dict[str, Holding] = field(default_factory=dict)
    contributed: dict[str, float] = field(default_factory=dict)
    parked: dict[str, float] = field(default_factory=dict)
    insured: list[str] = field(default_factory=list)          # holding half right now
    realized: float = 0.0
    last_buy: dict[str, str] = field(default_factory=dict)    # symbol -> ISO date
    last_run: str = ""                                         # ISO date of the last daily run

    def qty(self, symbol: str) -> int:
        return self.holdings.get(symbol, Holding()).qty

    def bought(self, symbol: str, qty: int, price: float, day: str) -> None:
        spend = qty * price
        from_parked = min(self.parked.get(symbol, 0.0), spend)
        self.parked[symbol] = self.parked.get(symbol, 0.0) - from_parked
        self.contributed[symbol] = self.contributed.get(symbol, 0.0) + spend - from_parked
        holding = self.holdings.setdefault(symbol, Holding())
        holding.qty += qty
        holding.cost += spend
        self.last_buy[symbol] = day

    def sold(self, symbol: str, qty: int, price: float, *, park: bool) -> None:
        """Book a sale. Parked proceeds stay with the robot; the rest go back to the main book."""
        holding = self.holdings[symbol]
        qty = min(qty, holding.qty)
        average = holding.cost / holding.qty
        proceeds = qty * price
        self.realized += qty * (price - average)
        holding.qty -= qty
        holding.cost -= qty * average
        if park:
            self.parked[symbol] = self.parked.get(symbol, 0.0) + proceeds
            if symbol not in self.insured:
                self.insured.append(symbol)
        else:
            self.contributed[symbol] = self.contributed.get(symbol, 0.0) - proceeds
        if holding.qty == 0:
            del self.holdings[symbol]

    def release(self, symbol: str) -> None:
        """Forget an index the robot no longer holds: its parked cash returns to the main book."""
        self.contributed[symbol] = self.contributed.get(symbol, 0.0) - self.parked.pop(symbol, 0.0)
        if symbol in self.insured:
            self.insured.remove(symbol)

    def recovered(self, symbol: str) -> None:
        if symbol in self.insured:
            self.insured.remove(symbol)

    def reconcile(self, held: dict[str, int]) -> None:
        """Shrink to what the broker still holds, if shares left outside the robot."""
        for symbol, holding in list(self.holdings.items()):
            broker_qty = held.get(symbol, 0)
            if broker_qty < holding.qty:
                if broker_qty <= 0:
                    del self.holdings[symbol]
                    continue
                holding.cost *= broker_qty / holding.qty
                holding.qty = broker_qty

    def profit(self, prices: dict[str, float]) -> float:
        value = sum(h.qty * prices[s] for s, h in self.holdings.items() if s in prices)
        return value + sum(self.parked.values()) - sum(self.contributed.values())


def read_ledger(tenant_dir: Path) -> Ledger:
    path = Path(tenant_dir) / LEDGER_FILE
    if not path.exists():
        return Ledger()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Ledger(
        holdings={s: Holding(int(h["qty"]), float(h["cost"])) for s, h in raw["holdings"].items()},
        contributed={str(k): float(v) for k, v in raw.get("contributed", {}).items()},
        parked={str(k): float(v) for k, v in raw.get("parked", {}).items()},
        insured=[str(s) for s in raw.get("insured", [])],
        realized=float(raw.get("realized", 0.0)),
        last_buy={str(k): str(v) for k, v in raw.get("last_buy", {}).items()},
        last_run=str(raw.get("last_run", "")),
    )


def write_ledger(tenant_dir: Path, ledger: Ledger) -> None:
    write_json_atomic(Path(tenant_dir) / LEDGER_FILE, asdict(ledger))


def book_equity(equity: float, ledger: Ledger, prices: dict[str, float]) -> float:
    """The main robot's equity: the account without the index robot's profit or loss."""
    return equity - ledger.profit(prices)

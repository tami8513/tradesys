"""Did the analysts' calls make money? Their past picks, priced again today.

Every scout report and every full analysis is saved with the price on the day
it was made. Once a pick is at least a week old, its return since then is set
against the S&P 500's over the same days. That is the analysts' report card:
not how convincing a call sounded, but what the price did after it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

from tradesys.core.fileio import locked
from tradesys.core.models import Bar

ANALYSES_FILE = "analyses.jsonl"
MATURE_DAYS = 7
LOOKBACK_DAYS = 120
BENCHMARK = "SPY"


@dataclass(frozen=True, slots=True)
class Call:
    day: date
    symbol: str
    label: str        # scout grade (A/B/C/S/F) or analysis level (go/careful/no)
    price: float      # the price the call was made at


@dataclass(frozen=True, slots=True)
class Outcome:
    call: Call
    ret: float        # % since the call
    bench: float      # % for the S&P 500 over the same days

    @property
    def beat(self) -> bool:
        return self.ret > self.bench


def log_analysis(platform_dir: Path, day: date, symbol: str, level: str, price: float) -> None:
    """Record one full analysis, once per stock per day."""
    path = Path(platform_dir) / ANALYSES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        if any(c.day == day and c.symbol == symbol for c in read_analyses(platform_dir)):
            return
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"day": day.isoformat(), "symbol": symbol,
                                     "level": level, "price": price}) + "\n")


def read_analyses(platform_dir: Path) -> list[Call]:
    path = Path(platform_dir) / ANALYSES_FILE
    if not path.exists():
        return []
    calls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            raw = json.loads(line)
            calls.append(Call(date.fromisoformat(raw["day"]), raw["symbol"], raw["level"], float(raw["price"])))
        except (ValueError, KeyError, TypeError):
            continue
    return calls


def scout_calls(platform_dir: Path, today: date) -> list[Call]:
    """Every name the talent scout put on a shortlist in the last few months."""
    calls = []
    for path in sorted(Path(platform_dir).glob("scout_*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            # The day it was written, after a close: its prices are that close.
            day = datetime.fromisoformat(raw["made_at"]).date()
            if day < today - timedelta(days=LOOKBACK_DAYS):
                continue
            for report in raw["reports"]:
                calls.append(Call(day, report["symbol"], report["grade"], float(report["price"])))
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return calls


def research_calls(platform_dir: Path, today: date) -> list[Call]:
    """Every stock and fund the market researcher named, at the price on the day it named it."""
    calls = []
    for path in sorted(Path(platform_dir).glob("research_*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            day = datetime.fromisoformat(raw["made_at"]).date()
            if day < today - timedelta(days=LOOKBACK_DAYS):
                continue
            named = {s for theme in raw["themes"] for s in theme["stocks"] + theme["funds"]}
            calls += [Call(day, s, "R", float(raw["prices"][s])) for s in sorted(named) if s in raw["prices"]]
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return calls


def _close_on_or_before(bars: Sequence[Bar], day: date) -> float | None:
    earlier = [b.close for b in bars if b.ts.date() <= day]
    return earlier[-1] if earlier else None


def outcomes(calls: Sequence[Call], bars: dict[str, Sequence[Bar]], today: date) -> list[Outcome]:
    """Calls at least a week old, with their return and the index's since."""
    benchmark = bars.get(BENCHMARK) or []
    results = []
    for call in calls:
        series = bars.get(call.symbol)
        if not series or not benchmark or (today - call.day).days < MATURE_DAYS or call.price <= 0:
            continue
        bench_then = _close_on_or_before(benchmark, call.day)
        if not bench_then:
            continue
        results.append(Outcome(
            call,
            (series[-1].close / call.price - 1) * 100,
            (benchmark[-1].close / bench_then - 1) * 100,
        ))
    return results

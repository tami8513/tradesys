"""Earnings calendar — the data behind the gap-risk filter.

Why this exists: every stop in this system is ATR-based, and an ATR stop does not
protect against an overnight earnings gap. A position stopped at 52.88 can open at
44 the morning after a report, and the monitor sells at the open, not the stop. The
realized loss is then several times what the Risk Manager sized for. Knowing when a
report is due is the only way to avoid that class of loss.

Two implementations:

  * StaticEarningsCalendar — an operator-maintained YAML file. Zero dependencies,
    works offline, deterministic in tests. Good enough for a handful of tickers.
  * FinnhubEarningsCalendar — a real vendor feed for when the watchlist grows.

Both answer the same question and return None for "I do not know", which the Risk
Manager treats as a blocking condition rather than an all-clear.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EarningsLookup:
    """The three states this question actually has.

    A plain `date | None` conflates "an ETF that never reports" with "a symbol
    nobody entered". Under a fail-closed filter that ambiguity makes every ETF
    permanently untradeable, so coverage is carried explicitly.
    """

    covered: bool
    next_date: date | None = None

    @classmethod
    def unknown(cls) -> "EarningsLookup":
        """Not covered, or the feed could not answer. Blocks entries."""
        return cls(covered=False)

    @classmethod
    def none_scheduled(cls) -> "EarningsLookup":
        """Covered, nothing upcoming — an ETF, an index, or a quiet period."""
        return cls(covered=True)

    @classmethod
    def scheduled(cls, when: date) -> "EarningsLookup":
        return cls(covered=True, next_date=when)


class EarningsCalendar(Protocol):
    def lookup(self, symbol: str, on_or_after: date) -> EarningsLookup:
        """Coverage and next report in one call.

        One call, not two: a vendor-backed calendar would otherwise make a
        network round trip per question, and answering them at different dates
        would cache under different keys.
        """
        ...


class StaticEarningsCalendar:
    """File-backed calendar the operator maintains by hand.

    Format (one symbol per key, a list of ISO dates):

        RIVN: [2026-09-15, 2026-12-08]
        KULR: [2026-09-22]

    A symbol absent from the file is unknown, not clear. Reloads when the file's
    mtime changes, so editing it does not require a restart.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._cache: dict[str, list[date]] = {}
        self._mtime: float | None = None

    def _load(self) -> dict[str, list[date]]:
        if not self._path.exists():
            self._cache = {}
            self._mtime = None
            return self._cache

        mtime = self._path.stat().st_mtime
        if self._mtime == mtime:
            return self._cache

        try:
            import yaml

            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except Exception:
            # A malformed calendar must not be read as "no earnings scheduled".
            # Empty cache means every symbol is unknown, which blocks rather than
            # silently permitting trades the filter was meant to stop.
            self._cache = {}
            self._mtime = mtime
            return self._cache

        parsed: dict[str, list[date]] = {}
        for symbol, values in raw.items():
            if not isinstance(values, list):
                values = [values]
            dates: list[date] = []
            for value in values:
                parsed_date = _coerce_date(value)
                if parsed_date is not None:
                    dates.append(parsed_date)
            # An explicitly listed symbol with no dates means "known, never
            # reports" (ETFs, indices). Absent from the file means "unknown".
            parsed[str(symbol).upper()] = sorted(dates)

        self._cache = parsed
        self._mtime = mtime
        return self._cache

    def lookup(self, symbol: str, on_or_after: date) -> EarningsLookup:
        calendar = self._load()
        key = symbol.upper()
        if key not in calendar:
            return EarningsLookup.unknown()
        upcoming = next((d for d in calendar[key] if d >= on_or_after), None)
        return (
            EarningsLookup.scheduled(upcoming)
            if upcoming
            else EarningsLookup.none_scheduled()
        )

    def known_symbols(self) -> set[str]:
        return set(self._load())


class FinnhubEarningsCalendar:
    """Vendor-backed calendar. Requires a Finnhub API key and httpx.

    Results are cached per (symbol, month) so a cycle over a watchlist costs at
    most one request per symbol per month rather than one per evaluation.
    """

    _BASE = "https://finnhub.io/api/v1/calendar/earnings"

    def __init__(self, api_key: str, *, timeout: float = 10.0, horizon_days: int = 120) -> None:
        if not api_key:
            raise ValueError("a Finnhub API key is required")
        self._api_key = api_key
        self._timeout = timeout
        self._horizon_days = horizon_days
        self._cache: dict[tuple[str, str], list[date]] = {}

    def lookup(self, symbol: str, on_or_after: date) -> EarningsLookup:
        from datetime import timedelta

        key = (symbol.upper(), on_or_after.strftime("%Y-%m"))
        if key not in self._cache:
            try:
                import httpx

                response = httpx.get(
                    self._BASE,
                    params={
                        "symbol": symbol.upper(),
                        "from": on_or_after.isoformat(),
                        "to": (on_or_after + timedelta(days=self._horizon_days)).isoformat(),
                        "token": self._api_key,
                    },
                    timeout=self._timeout,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception:
                # Do not cache a failure: an outage should be retried next cycle,
                # not frozen into a month-long "unknown" for this symbol. Until
                # then this reads as uncovered, which blocks rather than passes.
                return EarningsLookup.unknown()

            dates: list[date] = []
            for item in payload.get("earningsCalendar") or []:
                parsed = _coerce_date(item.get("date"))
                if parsed is not None:
                    dates.append(parsed)
            self._cache[key] = sorted(dates)

        upcoming = next((d for d in self._cache[key] if d >= on_or_after), None)
        return (
            EarningsLookup.scheduled(upcoming)
            if upcoming
            else EarningsLookup.none_scheduled()
        )


class NullEarningsCalendar:
    """Answers 'unknown' for everything. The default when no source is configured."""

    def lookup(self, symbol: str, on_or_after: date) -> EarningsLookup:
        del symbol, on_or_after
        return EarningsLookup.unknown()


def _coerce_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def days_until(target: date, reference: date) -> int:
    return (target - reference).days

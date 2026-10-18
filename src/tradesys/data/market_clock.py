"""Is the US equity market open, and when does it next open?

Alpaca's clock endpoint knows holidays, half days and session changes (including
extended sessions when exchanges adopt them), so it is preferred. Without keys,
or when the call fails, a plain 09:30-16:00 New York weekday schedule is used —
it misses holidays, which only means a few idle cycles on those days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class MarketStatus:
    is_open: bool
    next_open: datetime
    source: str


class MarketClock(Protocol):
    def status(self) -> MarketStatus: ...


class RegularHoursClock:
    """Weekdays 09:30-16:00 New York. No holiday calendar."""

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))

    def status(self) -> MarketStatus:
        local = self._now().astimezone(NEW_YORK)
        opens, closes = time(9, 30), time(16, 0)
        is_open = local.weekday() < 5 and opens <= local.time() < closes

        day = local.date()
        if local.weekday() < 5 and local.time() < opens:
            candidate = day
        else:
            candidate = day + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        next_open = datetime.combine(candidate, opens, tzinfo=NEW_YORK)
        return MarketStatus(is_open=is_open, next_open=next_open, source="regular-hours")


class AlpacaMarketClock:
    def __init__(
        self,
        key_id: str,
        secret_key: str,
        *,
        base_url: str = "https://paper-api.alpaca.markets",
        fallback: MarketClock | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._url = base_url.rstrip("/") + "/v2/clock"
        self._fallback = fallback or RegularHoursClock()
        self._timeout = timeout

    def status(self) -> MarketStatus:
        try:
            import httpx

            response = httpx.get(self._url, headers=self._headers, timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
            return MarketStatus(
                is_open=bool(payload["is_open"]),
                next_open=datetime.fromisoformat(str(payload["next_open"])),
                source="alpaca",
            )
        except Exception:
            return self._fallback.status()


def market_clock(key_id: str, secret_key: str) -> MarketClock:
    if key_id and secret_key:
        return AlpacaMarketClock(key_id, secret_key)
    return RegularHoursClock()

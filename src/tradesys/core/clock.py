from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    """Deterministic clock for tests and replayed backtests."""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._moment = self._moment + timedelta(seconds=seconds)

    def set(self, moment: datetime) -> None:
        self._moment = moment


def trading_day(clock: Clock) -> date:
    return clock.now().date()

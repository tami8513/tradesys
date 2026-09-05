from __future__ import annotations

from typing import Sequence

from tradesys.core.models import Bar


def closes(bars: Sequence[Bar]) -> list[float]:
    return [b.close for b in bars]


def sma(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for previous, current in zip(values[-period - 1 : -1], values[-period:]):
        change = current - previous
        if change >= 0:
            gains += change
        else:
            losses -= change
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def atr(bars: Sequence[Bar], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    true_ranges: list[float] = []
    for previous, current in zip(bars[-period - 1 : -1], bars[-period:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(true_ranges) / period


def donchian_high(bars: Sequence[Bar], period: int, offset: int = 1) -> float | None:
    """Highest high over `period` bars, excluding the most recent `offset` bars."""
    window = bars[-(period + offset) : -offset] if offset else bars[-period:]
    if len(window) < period:
        return None
    return max(b.high for b in window)


def average_volume(bars: Sequence[Bar], period: int = 20) -> float | None:
    if len(bars) < period:
        return None
    return sum(b.volume for b in bars[-period:]) / period

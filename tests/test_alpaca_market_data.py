"""Alpaca market data client, exercised offline against a fake httpx.

The two failure modes guarded here would both have left a live-data demo silently
idle: no `start` (Alpaca returns only today's bars, the Scout needs 60+), and
passing `limit` alongside `start` (Alpaca returns the *oldest* bars in the window).
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradesys.core.config import Settings
from tradesys.data.market_data import AlpacaMarketData, SyntheticMarketData, _lookback_days
from tradesys.orchestration.engine import TradingSystem

NOW = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)


def bar(day: int, close: float) -> dict:
    ts = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)).isoformat()
    return {"t": ts.replace("+00:00", "Z"), "o": close, "h": close, "l": close, "c": close, "v": 1000}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def install(monkeypatch, pages: list[dict]):
    calls: list[dict] = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {}), "headers": headers})
        return FakeResponse(pages[min(len(calls) - 1, len(pages) - 1)])

    module = types.ModuleType("httpx")
    module.get = fake_get
    monkeypatch.setitem(sys.modules, "httpx", module)
    return calls


def client() -> AlpacaMarketData:
    return AlpacaMarketData("key", "secret", now=lambda: NOW)


def test_history_sends_an_explicit_start(monkeypatch):
    calls = install(monkeypatch, [{"bars": [bar(i, 10 + i) for i in range(200)]}])
    client().history("RIVN", timeframe="1Day", limit=180)

    start = datetime.strptime(calls[0]["params"]["start"], "%Y-%m-%dT%H:%M:%SZ")
    assert start.replace(tzinfo=timezone.utc) < NOW - timedelta(days=180)


def test_history_returns_the_latest_bars_not_the_oldest(monkeypatch):
    install(monkeypatch, [{"bars": [bar(i, float(i)) for i in range(300)]}])
    bars = client().history("RIVN", timeframe="1Day", limit=180)

    assert len(bars) == 180
    assert bars[-1].close == 299.0
    assert bars[0].close == 120.0


def test_history_follows_pagination(monkeypatch):
    calls = install(
        monkeypatch,
        [
            {"bars": [bar(i, float(i)) for i in range(100)], "next_page_token": "p2"},
            {"bars": [bar(i, float(i)) for i in range(100, 150)], "next_page_token": None},
        ],
    )
    bars = client().history("RIVN", timeframe="1Day", limit=500)

    assert len(calls) == 2
    assert calls[1]["params"]["page_token"] == "p2"
    assert len(bars) == 150


def test_history_uses_full_tape_volume_with_the_free_plan_delay(monkeypatch):
    """Bars come from SIP, ending 15+ minutes ago, which the free plan allows.

    Measured on a real account: IEX alone carried 4-5% of RIVN's daily volume.
    """
    calls = install(monkeypatch, [{"bars": []}])
    client().history("RIVN", timeframe="1Day", limit=10)
    params = calls[0]["params"]
    assert params["feed"] == "sip"
    assert params["adjustment"] == "split"
    end = datetime.strptime(params["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert NOW - end >= timedelta(minutes=15)


def test_last_price_stays_real_time_on_iex(monkeypatch):
    calls = install(monkeypatch, [{"trade": {"p": 1.0}}])
    client().last_price("RIVN")
    assert calls[0]["params"]["feed"] == "iex"


def test_paid_sip_plan_needs_no_delay(monkeypatch):
    calls = install(monkeypatch, [{"bars": []}])
    AlpacaMarketData("k", "s", feed="sip", now=lambda: NOW).history(
        "RIVN", timeframe="1Day", limit=10
    )
    assert "end" not in calls[0]["params"]


def test_last_price_reads_the_latest_trade(monkeypatch):
    install(monkeypatch, [{"trade": {"p": 55.95}}])
    assert client().last_price("RIVN") == 55.95


@pytest.mark.parametrize(
    "timeframe,limit,minimum",
    [("1Day", 200, 280), ("1Hour", 100, 15), ("15Min", 100, 3), ("1Week", 52, 350)],
)
def test_lookback_window_covers_the_requested_bars(timeframe, limit, minimum):
    assert _lookback_days(timeframe, limit) >= minimum


def test_unsupported_timeframe_is_rejected():
    with pytest.raises(ValueError):
        _lookback_days("3Fortnights", 10)


# ---------------------------------------------------------------- engine routing

def settings(tmp_path: Path, **overrides) -> Settings:
    root = Path(__file__).resolve().parent.parent
    return Settings(clients_dir=root / "clients", data_dir=tmp_path, **overrides)


def test_paper_tenant_uses_synthetic_prices_without_platform_keys(tmp_path):
    system = TradingSystem(settings(tmp_path))
    session = system.for_client("demo_showcase")
    try:
        assert isinstance(session.market_data, SyntheticMarketData)
        assert system.market_data_source() == "synthetic"
    finally:
        session.close()


def test_paper_tenant_uses_live_prices_with_platform_keys(tmp_path):
    """Live prices, but the balance still lives in our own paper ledger."""
    from tradesys.brokers.paper import PaperBroker

    system = TradingSystem(
        settings(tmp_path, market_data_key_id="k", market_data_secret_key="s")
    )
    session = system.for_client("demo_showcase")
    try:
        assert isinstance(session.market_data, AlpacaMarketData)
        assert isinstance(session.broker, PaperBroker)
        assert system.market_data_source() == "alpaca:iex"
    finally:
        session.close()

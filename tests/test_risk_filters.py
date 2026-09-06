"""Earnings-gap and market-regime filters.

Both are risk controls, so the important cases are not the happy paths — they are
what happens when the data is missing. A filter that silently passes when its feed
is down is worse than no filter, because it is trusted.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import make_account, make_candidate, make_profile, make_stats

from tradesys.agents.risk_manager import RiskManager
from tradesys.clients.profile import BrokerConfig, ClientProfile, RiskProfile, WatchlistEntry
from tradesys.core.enums import SetupType
from tradesys.core.tenancy import TenantContext
from tradesys.data.earnings import (
    EarningsLookup,
    NullEarningsCalendar,
    StaticEarningsCalendar,
)
from tradesys.data.regime import MarketRegime, assess_regime
from tradesys.data.market_data import SyntheticMarketData
from tradesys.persistence.journal import Journal


class FakeCalendar:
    """Covers exactly the symbols it was given; a symbol may map to None."""

    def __init__(self, dates: dict[str, date | None] | None = None) -> None:
        self._dates = dates or {}

    def lookup(self, symbol: str, on_or_after: date) -> EarningsLookup:
        key = symbol.upper()
        if key not in self._dates:
            return EarningsLookup.unknown()
        found = self._dates[key]
        if found is None or found < on_or_after:
            return EarningsLookup.none_scheduled()
        return EarningsLookup.scheduled(found)


def build_profile(
    *,
    earnings_window: int | None = None,
    regime_blocked: frozenset[SetupType] = frozenset(),
    regime_period: int = 200,
) -> ClientProfile:
    base = make_profile()
    risk = RiskProfile(
        max_drawdown_pct=base.risk.max_drawdown_pct,
        max_daily_loss_pct=base.risk.max_daily_loss_pct,
        risk_per_trade_pct=base.risk.risk_per_trade_pct,
        max_position_pct=base.risk.max_position_pct,
        max_open_positions=base.risk.max_open_positions,
        max_trades_per_day=base.risk.max_trades_per_day,
        min_reward_risk=base.risk.min_reward_risk,
        avoid_earnings_within_days=earnings_window,
        regime_symbol="SPY",
        regime_sma_period=regime_period,
        regime_blocked_setups=regime_blocked,
    )
    return ClientProfile(
        client_id=base.client_id,
        display_name=base.display_name,
        watchlist=base.watchlist,
        risk=risk,
        broker=BrokerConfig(name="paper", paper=True),
    )


def build_risk(tmp_path, clock, profile, calendar=None) -> RiskManager:
    tenant = TenantContext.create(profile.client_id, tmp_path)
    journal = Journal(tenant)
    return RiskManager(tenant, profile, journal, clock, earnings=calendar)


# ------------------------------------------------------------------ earnings

def test_filter_is_inert_when_not_configured(tmp_path, clock):
    risk = build_risk(tmp_path, clock, build_profile(), NullEarningsCalendar())
    decision = risk.evaluate(make_candidate(), make_account(), [], make_stats())
    assert decision.approved


def test_entry_blocked_inside_the_earnings_window(tmp_path, clock):
    report = clock.now().date() + timedelta(days=2)
    risk = build_risk(
        tmp_path,
        clock,
        build_profile(earnings_window=3),
        FakeCalendar({"RIVN": report}),
    )
    decision = risk.evaluate(make_candidate(), make_account(), [], make_stats())

    assert not decision.approved
    assert any("reports in 2d" in reason for reason in decision.reasons)
    assert any("gap" in reason for reason in decision.reasons)


def test_entry_allowed_outside_the_earnings_window(tmp_path, clock):
    report = clock.now().date() + timedelta(days=30)
    risk = build_risk(
        tmp_path,
        clock,
        build_profile(earnings_window=3),
        FakeCalendar({"RIVN": report}),
    )
    assert risk.evaluate(make_candidate(), make_account(), [], make_stats()).approved


def test_uncovered_symbol_blocks_rather_than_passes(tmp_path, clock):
    """The whole point of the filter is not knowing what is coming."""
    risk = build_risk(
        tmp_path, clock, build_profile(earnings_window=3), FakeCalendar({})
    )
    decision = risk.evaluate(make_candidate(), make_account(), [], make_stats())

    assert not decision.approved
    assert any("no earnings coverage" in reason for reason in decision.reasons)


def test_symbol_that_never_reports_is_tradeable(tmp_path, clock):
    """An ETF is covered-with-nothing-scheduled, not unknown.

    Regression: both cases used to return None, so enabling the filter made every
    ETF permanently untradeable.
    """
    risk = build_risk(
        tmp_path,
        clock,
        build_profile(earnings_window=3),
        FakeCalendar({"RIVN": None}),
    )
    assert risk.evaluate(make_candidate(), make_account(), [], make_stats()).approved


def test_report_today_is_inside_the_window(tmp_path, clock):
    risk = build_risk(
        tmp_path,
        clock,
        build_profile(earnings_window=0),
        FakeCalendar({"RIVN": clock.now().date()}),
    )
    assert not risk.evaluate(make_candidate(), make_account(), [], make_stats()).approved


# -------------------------------------------------------------------- regime

def make_regime(price: float, average: float) -> MarketRegime:
    return MarketRegime(
        symbol="SPY",
        price=price,
        average=average,
        period=200,
        as_of=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )


def test_gated_setup_blocked_when_market_is_risk_off(tmp_path, clock):
    risk = build_risk(
        tmp_path, clock, build_profile(regime_blocked=frozenset({SetupType.BREAKOUT}))
    )
    decision = risk.evaluate(
        make_candidate(setup=SetupType.BREAKOUT),
        make_account(),
        [],
        make_stats(),
        make_regime(price=90.0, average=100.0),
    )
    assert not decision.approved
    assert any("risk-off" in reason for reason in decision.reasons)


def test_gated_setup_allowed_when_market_is_risk_on(tmp_path, clock):
    risk = build_risk(
        tmp_path, clock, build_profile(regime_blocked=frozenset({SetupType.BREAKOUT}))
    )
    decision = risk.evaluate(
        make_candidate(setup=SetupType.BREAKOUT),
        make_account(),
        [],
        make_stats(),
        make_regime(price=110.0, average=100.0),
    )
    assert decision.approved


def test_ungated_setup_ignores_a_risk_off_market(tmp_path, clock):
    """Only the setups the client listed are regime-gated, not the whole book."""
    risk = build_risk(
        tmp_path, clock, build_profile(regime_blocked=frozenset({SetupType.BREAKOUT}))
    )
    decision = risk.evaluate(
        make_candidate(setup=SetupType.PULLBACK, target=26.0),
        make_account(),
        [],
        make_stats(),
        make_regime(price=90.0, average=100.0),
    )
    assert decision.approved


def test_missing_regime_blocks_gated_setups(tmp_path, clock):
    risk = build_risk(
        tmp_path, clock, build_profile(regime_blocked=frozenset({SetupType.BREAKOUT}))
    )
    decision = risk.evaluate(
        make_candidate(setup=SetupType.BREAKOUT),
        make_account(),
        [],
        make_stats(),
        None,
    )
    assert not decision.approved
    assert any("regime unavailable" in reason for reason in decision.reasons)


def test_regime_is_ignored_entirely_when_no_setups_are_gated(tmp_path, clock):
    risk = build_risk(tmp_path, clock, build_profile())
    decision = risk.evaluate(
        make_candidate(), make_account(), [], make_stats(), make_regime(90.0, 100.0)
    )
    assert decision.approved


# ------------------------------------------------------- regime computation

def test_assess_regime_reads_price_against_its_average():
    regime = assess_regime(
        SyntheticMarketData(),
        "SPY",
        period=50,
        timeframe="1Day",
        now=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )
    assert regime is not None
    assert regime.symbol == "SPY"
    assert regime.risk_on == (regime.price >= regime.average)
    assert "SPY" in regime.describe()


def test_assess_regime_returns_none_without_enough_history():
    class ShortHistory:
        def history(self, symbol, *, timeframe, limit):
            return SyntheticMarketData().history(symbol, timeframe=timeframe, limit=5)[:5]

        def last_price(self, symbol):
            return 100.0

    assert (
        assess_regime(
            ShortHistory(),
            "SPY",
            period=200,
            timeframe="1Day",
            now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        )
        is None
    )


def test_assess_regime_survives_a_data_outage():
    class Broken:
        def history(self, symbol, *, timeframe, limit):
            raise RuntimeError("feed down")

        def last_price(self, symbol):
            raise RuntimeError("feed down")

    assert (
        assess_regime(
            Broken(),
            "SPY",
            period=200,
            timeframe="1Day",
            now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        )
        is None
    )


# ------------------------------------------------------- static calendar file

def test_static_calendar_reads_and_reloads(tmp_path):
    path = tmp_path / "earnings.yaml"
    path.write_text("RIVN: [2026-09-15, 2026-12-08]\nKULR: 2026-09-22\n", encoding="utf-8")
    calendar = StaticEarningsCalendar(path)

    assert calendar.lookup("RIVN", date(2026, 9, 1)).next_date == date(2026, 9, 15)
    assert calendar.lookup("RIVN", date(2026, 10, 1)).next_date == date(2026, 12, 8)
    assert calendar.lookup("kulr", date(2026, 9, 1)).next_date == date(2026, 9, 22)
    assert not calendar.lookup("SPCE", date(2026, 9, 1)).covered


def test_malformed_calendar_reports_unknown_not_clear(tmp_path):
    """A broken file must not read as 'no earnings scheduled'."""
    path = tmp_path / "earnings.yaml"
    path.write_text("this: [is: not: valid: yaml", encoding="utf-8")
    assert not StaticEarningsCalendar(path).lookup("RIVN", date(2026, 9, 1)).covered


def test_missing_calendar_file_is_not_an_error(tmp_path):
    calendar = StaticEarningsCalendar(tmp_path / "nope.yaml")
    assert not calendar.lookup("RIVN", date(2026, 9, 1)).covered


def test_static_calendar_separates_never_reports_from_unknown(tmp_path):
    path = tmp_path / "earnings.yaml"
    path.write_text("RIVN: [2026-11-04]\nSQQQ: []\n", encoding="utf-8")
    calendar = StaticEarningsCalendar(path)

    assert calendar.lookup("RIVN", date(2026, 9, 1)).covered
    assert calendar.lookup("SQQQ", date(2026, 9, 1)).covered
    assert not calendar.lookup("KULR", date(2026, 9, 1)).covered
    assert calendar.lookup("SQQQ", date(2026, 9, 1)).next_date is None


# ------------------------------------------------------------------- finnhub

class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "fake"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def install_fake_httpx(monkeypatch, responder):
    """Swap httpx.get for a recorder so the vendor client can be tested offline."""
    import types

    calls: list[dict] = []

    def fake_get(url, params=None, timeout=None, **kwargs):
        calls.append({"url": url, "params": dict(params or {})})
        return responder(params or {})

    module = types.ModuleType("httpx")
    module.get = fake_get
    monkeypatch.setitem(__import__("sys").modules, "httpx", module)
    return calls


def test_finnhub_parses_a_real_shaped_payload(monkeypatch):
    from tradesys.data.earnings import FinnhubEarningsCalendar

    payload = {
        "earningsCalendar": [
            {"date": "2026-11-04", "symbol": "RIVN", "epsEstimate": -0.65},
            {"date": "2027-02-19", "symbol": "RIVN", "epsEstimate": -0.55},
        ]
    }
    install_fake_httpx(monkeypatch, lambda params: FakeResponse(payload))

    calendar = FinnhubEarningsCalendar("fake-key-for-tests")
    found = calendar.lookup("RIVN", date(2026, 11, 1))

    assert found.covered
    assert found.next_date == date(2026, 11, 4)


def test_finnhub_empty_result_means_covered_not_unknown(monkeypatch):
    """A successful 'nothing scheduled' must not block the symbol."""
    from tradesys.data.earnings import FinnhubEarningsCalendar

    install_fake_httpx(monkeypatch, lambda params: FakeResponse({"earningsCalendar": []}))

    found = FinnhubEarningsCalendar("fake-key-for-tests").lookup("SQQQ", date(2026, 11, 1))
    assert found.covered
    assert found.next_date is None


def test_finnhub_outage_reads_as_unknown_and_is_not_cached(monkeypatch):
    """An outage must block, and must be retried rather than frozen for a month."""
    from tradesys.data.earnings import FinnhubEarningsCalendar

    state = {"fail": True}

    def responder(params):
        if state["fail"]:
            raise RuntimeError("connection reset")
        return FakeResponse({"earningsCalendar": [{"date": "2026-11-04"}]})

    calls = install_fake_httpx(monkeypatch, responder)
    calendar = FinnhubEarningsCalendar("fake-key-for-tests")

    assert not calendar.lookup("RIVN", date(2026, 11, 1)).covered

    state["fail"] = False
    recovered = calendar.lookup("RIVN", date(2026, 11, 1))
    assert recovered.covered and recovered.next_date == date(2026, 11, 4)
    assert len(calls) == 2  # retried, not served from a poisoned cache


def test_finnhub_caches_within_a_month(monkeypatch):
    from tradesys.data.earnings import FinnhubEarningsCalendar

    calls = install_fake_httpx(
        monkeypatch, lambda params: FakeResponse({"earningsCalendar": [{"date": "2026-11-04"}]})
    )
    calendar = FinnhubEarningsCalendar("fake-key-for-tests")

    calendar.lookup("RIVN", date(2026, 11, 1))
    calendar.lookup("RIVN", date(2026, 11, 20))
    assert len(calls) == 1

    calendar.lookup("RIVN", date(2026, 12, 1))
    assert len(calls) == 2


def test_finnhub_never_puts_the_key_in_the_url_path(monkeypatch):
    """The token belongs in params, where it will not land in a path-based log."""
    from tradesys.data.earnings import FinnhubEarningsCalendar

    calls = install_fake_httpx(
        monkeypatch, lambda params: FakeResponse({"earningsCalendar": []})
    )
    FinnhubEarningsCalendar("super-secret-key").lookup("RIVN", date(2026, 11, 1))

    assert "super-secret-key" not in calls[0]["url"]
    assert calls[0]["params"]["token"] == "super-secret-key"


def test_finnhub_requires_a_key():
    from tradesys.data.earnings import FinnhubEarningsCalendar

    with pytest.raises(ValueError):
        FinnhubEarningsCalendar("")

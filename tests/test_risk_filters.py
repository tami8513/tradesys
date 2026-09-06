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
from tradesys.data.earnings import NullEarningsCalendar, StaticEarningsCalendar
from tradesys.data.regime import MarketRegime, assess_regime
from tradesys.data.market_data import SyntheticMarketData
from tradesys.persistence.journal import Journal


class FakeCalendar:
    """Covers exactly the symbols it was given; a symbol may map to None."""

    def __init__(self, dates: dict[str, date | None] | None = None) -> None:
        self._dates = dates or {}

    def next_earnings(self, symbol: str, on_or_after: date) -> date | None:
        found = self._dates.get(symbol.upper())
        if found is None or found < on_or_after:
            return None
        return found

    def is_known(self, symbol: str) -> bool:
        return symbol.upper() in self._dates


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

    assert calendar.next_earnings("RIVN", date(2026, 9, 1)) == date(2026, 9, 15)
    assert calendar.next_earnings("RIVN", date(2026, 10, 1)) == date(2026, 12, 8)
    assert calendar.next_earnings("kulr", date(2026, 9, 1)) == date(2026, 9, 22)
    assert calendar.next_earnings("SPCE", date(2026, 9, 1)) is None


def test_malformed_calendar_reports_unknown_not_clear(tmp_path):
    """A broken file must not read as 'no earnings scheduled'."""
    path = tmp_path / "earnings.yaml"
    path.write_text("this: [is: not: valid: yaml", encoding="utf-8")
    assert StaticEarningsCalendar(path).next_earnings("RIVN", date(2026, 9, 1)) is None


def test_missing_calendar_file_is_not_an_error(tmp_path):
    calendar = StaticEarningsCalendar(tmp_path / "nope.yaml")
    assert calendar.next_earnings("RIVN", date(2026, 9, 1)) is None


def test_static_calendar_separates_never_reports_from_unknown(tmp_path):
    path = tmp_path / "earnings.yaml"
    path.write_text("RIVN: [2026-11-04]\nSQQQ: []\n", encoding="utf-8")
    calendar = StaticEarningsCalendar(path)

    assert calendar.is_known("RIVN") and calendar.is_known("SQQQ")
    assert not calendar.is_known("KULR")
    assert calendar.next_earnings("SQQQ", date(2026, 9, 1)) is None

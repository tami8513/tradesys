from __future__ import annotations

import pytest

from conftest import make_account, make_candidate, make_profile, make_stats

from tradesys.agents.risk_manager import RiskManager
from tradesys.clients.picks import MAX_PICKS, apply_picks, normalize, read_picks, write_picks
from tradesys.core.errors import ConfigError
from tradesys.core.tenancy import TenantContext
from tradesys.persistence.journal import Journal


@pytest.mark.parametrize("typed", ["nvda", " NVDA ", "BRK.B"])
def test_tickers_are_normalized(typed):
    assert normalize(typed) == typed.strip().upper()


@pytest.mark.parametrize("typed", ["", "TOOLONG", "NV DA", "1234", "../x"])
def test_bad_tickers_are_refused(typed):
    with pytest.raises(ConfigError):
        normalize(typed)


def test_picks_round_trip_without_duplicates(tmp_path):
    write_picks(tmp_path, ["nvda", "AAPL", "NVDA"])
    assert read_picks(tmp_path) == ["NVDA", "AAPL"]


def test_at_most_ten_picks(tmp_path):
    with pytest.raises(ConfigError):
        write_picks(tmp_path, [f"A{chr(65 + i)}" for i in range(MAX_PICKS + 1)])


def test_a_pick_joins_the_watchlist_and_is_pinned():
    profile = apply_picks(make_profile(symbols=("KULR",)), ["NVDA"])
    assert "NVDA" in profile.symbols
    assert profile.pinned_symbols == ("NVDA",)


def test_a_pick_still_goes_through_every_risk_check(tmp_path, clock):
    profile = apply_picks(make_profile(symbols=("KULR",)), ["NVDA"])
    tenant = TenantContext.create(profile.client_id, tmp_path)
    risk = RiskManager(tenant, profile, Journal(tenant), clock)

    approved = risk.evaluate(make_candidate(symbol="NVDA"), make_account(), [], make_stats())
    assert approved.approved

    too_cheap = risk.evaluate(
        make_candidate(symbol="NVDA", entry=0.5, stop=0.4, target=0.8),
        make_account(), [], make_stats(),
    )
    assert not too_cheap.approved

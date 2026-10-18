"""Capital floor — the 'protect the principal' track.

The property under test: however many trades are approved, if every open stop is
hit at once, equity stays at or above the floor. Gaps past a stop are outside
what sizing can control, which is why the product calls this a floor target and
not a guarantee.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import make_account, make_candidate, make_profile, make_stats

from tradesys.agents.risk_manager import RiskManager
from tradesys.clients.loader import parse_profile
from tradesys.clients.profile import BrokerConfig, ClientProfile, RiskProfile
from tradesys.core.enums import OrderType, Side, TimeInForce
from tradesys.core.errors import ConfigError
from tradesys.core.models import DayStats, OrderRequest
from tradesys.core.tenancy import TenantContext
from tradesys.persistence.journal import Journal

OPENED = datetime(2026, 9, 3, 14, 30, tzinfo=timezone.utc)


def profile_with_floor(
    floor_pct: float | None,
    *,
    ratchet: bool = False,
    per_trade_pct: float = 20.0,
) -> ClientProfile:
    base = make_profile()
    return ClientProfile(
        client_id=base.client_id,
        display_name=base.display_name,
        watchlist=base.watchlist,
        risk=RiskProfile(
            max_drawdown_pct=base.risk.max_drawdown_pct,
            max_daily_loss_pct=base.risk.max_daily_loss_pct,
            risk_per_trade_pct=base.risk.risk_per_trade_pct,
            max_position_pct=base.risk.max_position_pct,
            max_open_positions=base.risk.max_open_positions,
            max_trades_per_day=base.risk.max_trades_per_day,
            min_reward_risk=base.risk.min_reward_risk,
            capital_floor_pct=floor_pct,
            floor_ratchet=ratchet,
            floor_cushion_per_trade_pct=per_trade_pct,
        ),
        broker=BrokerConfig(name="paper", paper=True),
    )


def build(tmp_path, clock, profile) -> tuple[RiskManager, Journal]:
    tenant = TenantContext.create(profile.client_id, tmp_path)
    journal = Journal(tenant)
    return RiskManager(tenant, profile, journal, clock), journal


def record_open(journal: Journal, qty: int, entry: float = 20.0, stop: float = 18.0) -> None:
    candidate = make_candidate(entry=entry, stop=stop)
    order = OrderRequest(
        client_id="tal_dahan",
        symbol=candidate.symbol,
        side=Side.BUY,
        qty=qty,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        client_order_id=f"test-{qty}-{journal.open_risk()}",
        limit_price=entry,
        stop_loss=stop,
    )
    journal.open_trade(candidate, order, entry, OPENED)


# ------------------------------------------------------------------ sizing

def test_no_floor_leaves_sizing_unchanged(tmp_path, clock):
    risk, _ = build(tmp_path, clock, profile_with_floor(None))
    decision = risk.evaluate(make_candidate(), make_account(), [], make_stats())
    assert decision.approved and decision.qty == 250


def test_fresh_book_far_from_floor_sizes_normally(tmp_path, clock):
    # 5,000 cushion, 20% of it = 1,000 > the 500 per-trade budget: no change.
    risk, _ = build(tmp_path, clock, profile_with_floor(95.0))
    decision = risk.evaluate(make_candidate(), make_account(), [], make_stats())
    assert decision.approved and decision.qty == 250


def test_positions_shrink_as_equity_nears_the_floor(tmp_path, clock):
    risk, journal = build(tmp_path, clock, profile_with_floor(95.0))
    journal.roll_session(clock.now().date(), 100_000.0)  # base = 100k, floor = 95k

    decision = risk.evaluate(
        make_candidate(), make_account(equity=96_000.0), [], make_stats()
    )
    # cushion 1,000 -> 20% = 200 of risk -> 100 shares at 2.00 of stop distance
    assert decision.approved
    assert decision.qty == 100
    assert any("capital floor" in note for note in decision.reasons)


def test_entries_stop_at_the_floor(tmp_path, clock):
    risk, journal = build(tmp_path, clock, profile_with_floor(95.0))
    journal.roll_session(clock.now().date(), 100_000.0)

    decision = risk.evaluate(
        make_candidate(), make_account(equity=95_000.0), [], make_stats()
    )
    assert not decision.approved
    assert any("at capital floor" in reason for reason in decision.reasons)


def test_open_positions_consume_the_cushion(tmp_path, clock):
    risk, journal = build(tmp_path, clock, profile_with_floor(95.0))
    journal.roll_session(clock.now().date(), 100_000.0)
    record_open(journal, qty=2450)  # 4,900 at risk of a 5,000 cushion

    decision = risk.evaluate(make_candidate(), make_account(), [], make_stats())
    # 100 left, 20% = 20 of risk -> 10 shares
    assert decision.approved and decision.qty == 10


def test_ratchet_floor_follows_the_equity_peak(tmp_path, clock):
    risk, _ = build(tmp_path, clock, profile_with_floor(95.0, ratchet=True))
    stats = DayStats(
        client_id="tal_dahan", day=clock.now().date(), peak_equity=110_000.0
    )
    decision = risk.evaluate(
        make_candidate(), make_account(equity=105_000.0), [], stats
    )
    # floor 104,500, cushion 500 -> 100 of risk -> 50 shares
    assert decision.approved and decision.qty == 50


def test_capital_base_is_set_once(tmp_path, clock):
    _, journal = build(tmp_path, clock, profile_with_floor(95.0))
    journal.roll_session(clock.now().date(), 100_000.0)
    journal.roll_session(clock.now().date(), 140_000.0)
    assert journal.capital_base() == 100_000.0


# --------------------------------------------------------------- invariant

@pytest.mark.parametrize("floor_pct", [80.0, 90.0, 95.0])
def test_all_stops_hit_at_once_never_breaches_the_floor(tmp_path, clock, floor_pct):
    """The promise of the track, checked after every single approval."""
    risk, journal = build(tmp_path, clock, profile_with_floor(floor_pct))
    journal.roll_session(clock.now().date(), 100_000.0)
    account = make_account()

    approvals = 0
    for _ in range(60):
        decision = risk.evaluate(make_candidate(), account, [], make_stats())
        if not decision.approved:
            break
        record_open(journal, qty=decision.qty)
        approvals += 1

        state = risk.floor_state(account, make_stats())
        assert state.worst_case_equity >= state.floor - 1e-6

    assert approvals > 1
    final = risk.floor_state(account, make_stats())
    assert final.worst_case_equity >= final.floor - 1e-6


# ------------------------------------------------------------------ config

def config(**risk_overrides) -> dict:
    risk = {
        "max_drawdown_pct": 2.0,
        "max_daily_loss_pct": 1.5,
        "risk_per_trade_pct": 0.5,
        "max_position_pct": 12.0,
        "max_open_positions": 5,
        "max_trades_per_day": 3,
        "min_reward_risk": 2.0,
        **risk_overrides,
    }
    return {
        "client_id": "tal_dahan",
        "risk": risk,
        "watchlist": [{"symbol": "RIVN", "setups": ["breakout"]}],
    }


def test_named_track_sets_the_floor():
    profile = parse_profile(config(capital_track="capital_preservation"), Path("t.yaml"))
    assert profile.risk.capital_floor_pct == 95.0


def test_explicit_floor_overrides_the_track():
    profile = parse_profile(
        config(capital_track="growth", capital_floor_pct=85.0), Path("t.yaml")
    )
    assert profile.risk.capital_floor_pct == 85.0


def test_unknown_track_is_rejected():
    with pytest.raises(ConfigError):
        parse_profile(config(capital_track="yolo"), Path("t.yaml"))


def test_floor_of_100_percent_is_rejected():
    with pytest.raises(ConfigError):
        parse_profile(config(capital_floor_pct=100.0), Path("t.yaml"))


# ------------------------------------------------------------ client choice

def system_and_dir(tmp_path):
    from tradesys.core.config import Settings
    from tradesys.orchestration.engine import TradingSystem

    root = Path(__file__).resolve().parent.parent
    system = TradingSystem(Settings(clients_dir=root / "clients", data_dir=tmp_path))
    return system, system.tenant("tal_dahan").data_dir


def test_risk_level_sets_all_four_loss_limits_together(tmp_path):
    from tradesys.clients.profile import RISK_LEVELS
    from tradesys.clients.tracks import read_track, write_track

    system, tenant_dir = system_and_dir(tmp_path)
    write_track(tenant_dir, 2, 100_000.0)
    assert read_track(tenant_dir).level == 2

    session = system.for_client("tal_dahan")
    try:
        chosen, level = session.profile.risk, RISK_LEVELS[2]
        assert chosen.capital_floor_pct == level.capital_floor_pct
        assert chosen.capital_floor_base == 100_000.0
        assert chosen.risk_per_trade_pct == level.risk_per_trade_pct
        assert chosen.max_daily_loss_pct == level.max_daily_loss_pct
        assert chosen.max_drawdown_pct == level.max_drawdown_pct
        default = system.registry.load("tal_dahan").risk
        assert chosen.max_position_pct == default.max_position_pct
    finally:
        session.close()

    write_track(tenant_dir, None)
    assert read_track(tenant_dir) is None


def test_level_beyond_protection_needs_consent(tmp_path):
    from tradesys.clients.tracks import write_track

    with pytest.raises(ConfigError):
        write_track(tmp_path, 4, 100_000.0, max_level=5)
    write_track(tmp_path, 4, 100_000.0, max_level=5, acknowledged=True)


def test_level_above_the_operator_cap_cannot_be_written(tmp_path):
    from tradesys.clients.tracks import write_track

    with pytest.raises(ConfigError):
        write_track(tmp_path, 5, 100_000.0, acknowledged=True, max_level=3)


def test_operator_cap_wins_over_a_stored_choice():
    """A choice saved while permitted is clamped if the operator later lowers the cap."""
    from dataclasses import replace as dc_replace

    from tradesys.clients.profile import RISK_LEVELS
    from tradesys.clients.tracks import TrackChoice, apply_track

    capped = dc_replace(profile_with_floor(None), max_risk_level=3)
    applied = apply_track(capped, TrackChoice(5, 100_000.0, "", acknowledged=True))
    assert applied.risk.max_drawdown_pct == RISK_LEVELS[3].max_drawdown_pct


def test_unacknowledged_level_beyond_protection_is_not_applied():
    from dataclasses import replace as dc_replace

    from tradesys.clients.profile import RISK_LEVELS
    from tradesys.clients.tracks import TrackChoice, apply_track

    allowed = dc_replace(profile_with_floor(None), max_risk_level=5)
    applied = apply_track(allowed, TrackChoice(5, 100_000.0, "", acknowledged=False))
    assert applied.risk.capital_floor_pct == RISK_LEVELS[3].capital_floor_pct


def test_choice_saved_before_the_meter_reads_as_a_level(tmp_path):
    import json

    from tradesys.clients.tracks import TRACK_FILE, read_track

    (tmp_path / TRACK_FILE).write_text(
        json.dumps({"track": "capital_preservation", "base": 90_000.0}), encoding="utf-8"
    )
    assert read_track(tmp_path).level == 1


def test_unknown_level_cannot_be_written(tmp_path):
    from tradesys.clients.tracks import write_track

    with pytest.raises(ConfigError):
        write_track(tmp_path, 9, 100_000.0)


def test_a_level_needs_a_base():
    from tradesys.clients.tracks import write_track

    with pytest.raises(ConfigError):
        write_track(Path("."), 2, 0.0)


def test_choosing_a_track_after_losses_does_not_lock_the_account(tmp_path, clock):
    """Regression: the floor was measured from the first equity ever recorded.

    An account at 100k that fell to 85k and then chose a 95% track sat under its
    floor immediately and could never open a trade again. The floor is now
    measured from equity at the moment the track is chosen.
    """
    from tradesys.clients.tracks import TrackChoice, apply_track

    base_profile = profile_with_floor(None)
    risk, journal = build(tmp_path, clock, base_profile)
    journal.roll_session(clock.now().date(), 100_000.0)  # first equity ever: 100k

    chosen = apply_track(
        base_profile, TrackChoice(1, 85_000.0, "")
    )
    risk = RiskManager(risk.tenant, chosen, journal, clock)

    decision = risk.evaluate(
        make_candidate(), make_account(equity=85_000.0), [], make_stats()
    )
    assert decision.approved
    state = risk.floor_state(make_account(equity=85_000.0), make_stats())
    assert state.floor == pytest.approx(80_750.0)

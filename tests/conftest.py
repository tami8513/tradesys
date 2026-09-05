from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradesys.clients.profile import (
    BrokerConfig,
    ClientProfile,
    RiskProfile,
    WatchlistEntry,
)
from tradesys.core.clock import FrozenClock
from tradesys.core.enums import SetupType, Side
from tradesys.core.models import AccountSnapshot, Candidate, DayStats
from tradesys.core.tenancy import TenantContext
from tradesys.persistence.journal import Journal


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 9, 3, 14, 30, tzinfo=timezone.utc))


def make_profile(
    client_id: str = "tal_dahan",
    *,
    max_drawdown_pct: float = 2.0,
    risk_per_trade_pct: float = 0.5,
    max_position_pct: float = 12.0,
    max_trades_per_day: int = 3,
    min_reward_risk: float = 2.0,
    allow_short: bool = False,
    symbols: tuple[str, ...] = ("KULR", "RIVN"),
) -> ClientProfile:
    return ClientProfile(
        client_id=client_id,
        display_name=client_id,
        watchlist=tuple(
            WatchlistEntry(
                symbol=symbol,
                setups=frozenset(
                    {SetupType.BREAKOUT, SetupType.PULLBACK, SetupType.TREND_CONTINUATION}
                ),
            )
            for symbol in symbols
        ),
        risk=RiskProfile(
            max_drawdown_pct=max_drawdown_pct,
            max_daily_loss_pct=1.5,
            risk_per_trade_pct=risk_per_trade_pct,
            max_position_pct=max_position_pct,
            max_open_positions=5,
            max_trades_per_day=max_trades_per_day,
            min_reward_risk=min_reward_risk,
            allow_short=allow_short,
        ),
        broker=BrokerConfig(name="paper", paper=True),
    )


def make_candidate(
    client_id: str = "tal_dahan",
    *,
    symbol: str = "RIVN",
    setup: SetupType = SetupType.BREAKOUT,
    entry: float = 20.0,
    stop: float = 18.0,
    target: float = 26.0,
    side: Side = Side.BUY,
    avg_volume: float = 5_000_000.0,
) -> Candidate:
    return Candidate(
        client_id=client_id,
        symbol=symbol,
        setup=setup,
        side=side,
        entry=entry,
        stop=stop,
        target=target,
        confidence=0.6,
        rationale="test setup",
        features={"avg_volume": avg_volume},
    )


def make_account(client_id: str = "tal_dahan", equity: float = 100_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        client_id=client_id,
        equity=equity,
        cash=equity,
        buying_power=equity,
        taken_at=datetime(2026, 9, 3, 14, 30, tzinfo=timezone.utc),
    )


def make_stats(client_id: str = "tal_dahan", trades_opened: int = 0) -> DayStats:
    return DayStats(
        client_id=client_id,
        day=datetime(2026, 9, 3, tzinfo=timezone.utc).date(),
        trades_opened=trades_opened,
    )


@pytest.fixture
def tenant(tmp_path: Path) -> TenantContext:
    return TenantContext.create("tal_dahan", tmp_path)


@pytest.fixture
def journal(tenant: TenantContext) -> Journal:
    j = Journal(tenant)
    yield j
    j.close()

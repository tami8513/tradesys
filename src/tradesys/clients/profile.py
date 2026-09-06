from __future__ import annotations

from dataclasses import dataclass, field

from tradesys.core.enums import SetupType
from tradesys.core.errors import ConfigError
from tradesys.core.identity import validate_client_id


@dataclass(frozen=True, slots=True)
class RiskProfile:
    """Hard limits for one tenant. Every field is enforced by the Risk Manager.

    These are ceilings, not suggestions: no model output can widen them, and the
    Risk Manager is the only component permitted to read them.
    """

    max_drawdown_pct: float          # peak-to-current equity; breach halts the tenant
    max_daily_loss_pct: float        # realized loss vs. start-of-day equity
    risk_per_trade_pct: float        # equity risked between entry and stop
    max_position_pct: float          # notional cap for a single position
    max_open_positions: int
    max_trades_per_day: int
    min_reward_risk: float
    allow_short: bool = False
    allow_leverage: bool = False
    forbidden_setups: frozenset[SetupType] = frozenset()
    min_price: float = 1.0           # keeps sub-$1 tickers out by construction
    max_position_pct_of_adv: float = 0.02  # liquidity cap vs. average daily volume

    # --- gap risk -----------------------------------------------------------
    # An ATR stop cannot protect against an overnight earnings gap: the position
    # opens past the stop and the monitor sells at the open. None disables the
    # filter; any integer blocks entries within that many days of a report.
    avoid_earnings_within_days: int | None = None

    # --- market regime ------------------------------------------------------
    # Setups listed here are only permitted while `regime_symbol` trades at or
    # above its `regime_sma_period` average. Empty set disables the filter.
    regime_symbol: str = "SPY"
    regime_sma_period: int = 200
    regime_blocked_setups: frozenset[SetupType] = frozenset()

    def __post_init__(self) -> None:
        pcts = {
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_position_pct": self.max_position_pct,
        }
        for name, value in pcts.items():
            if not 0 < value <= 100:
                raise ConfigError(f"{name} must be in (0, 100], got {value}")
        if self.risk_per_trade_pct > self.max_daily_loss_pct:
            raise ConfigError(
                "risk_per_trade_pct exceeds max_daily_loss_pct: a single losing "
                "trade would breach the daily limit"
            )
        if self.max_open_positions < 1 or self.max_trades_per_day < 1:
            raise ConfigError("position and trade-count limits must be >= 1")
        if self.min_reward_risk < 1.0:
            raise ConfigError("min_reward_risk below 1.0 is not accepted")
        if self.avoid_earnings_within_days is not None and self.avoid_earnings_within_days < 0:
            raise ConfigError("avoid_earnings_within_days must be >= 0 when set")
        if self.regime_blocked_setups and self.regime_sma_period < 2:
            raise ConfigError("regime_sma_period must be at least 2 bars")
        if self.regime_blocked_setups and not self.regime_symbol:
            raise ConfigError("regime_blocked_setups requires a regime_symbol")


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    symbol: str
    setups: frozenset[SetupType]
    notes: str = ""
    max_position_pct: float | None = None   # tightens, never widens, the profile cap

    def __post_init__(self) -> None:
        if not self.symbol.isupper():
            object.__setattr__(self, "symbol", self.symbol.upper())


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    name: str                    # "paper" | "alpaca"
    paper: bool = True
    credential_ref: str = ""     # env-var *name*, never a secret value
    base_url: str | None = None

    def __post_init__(self) -> None:
        if any(token in self.credential_ref for token in ("-----", "sk_", "PK")):
            raise ConfigError("credential_ref looks like a secret value, not a name")


@dataclass(frozen=True, slots=True)
class ClientProfile:
    """Everything that makes one tenant's agents behave differently from another's."""

    client_id: str
    display_name: str
    watchlist: tuple[WatchlistEntry, ...]
    risk: RiskProfile
    broker: BrokerConfig
    timeframe: str = "1Day"
    lookback_bars: int = 120
    max_candidates_per_cycle: int = 5
    memory_top_k: int = 5
    monitor_interval_seconds: float = 60.0
    max_holding_days: int | None = None   # None disables the time stop
    tags: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_client_id(self.client_id)
        if not self.watchlist:
            raise ConfigError(f"client {self.client_id} has an empty watchlist")
        if self.monitor_interval_seconds <= 0:
            raise ConfigError("monitor_interval_seconds must be positive")
        if self.max_holding_days is not None and self.max_holding_days < 1:
            raise ConfigError("max_holding_days must be >= 1 when set")
        symbols = [e.symbol for e in self.watchlist]
        duplicates = {s for s in symbols if symbols.count(s) > 1}
        if duplicates:
            raise ConfigError(f"duplicate watchlist symbols: {sorted(duplicates)}")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(e.symbol for e in self.watchlist)

    def entry_for(self, symbol: str) -> WatchlistEntry | None:
        wanted = symbol.upper()
        return next((e for e in self.watchlist if e.symbol == wanted), None)

    def allows_setup(self, symbol: str, setup: SetupType) -> bool:
        """A setup must be enabled per-symbol *and* not globally forbidden."""
        if setup in self.risk.forbidden_setups:
            return False
        entry = self.entry_for(symbol)
        return entry is not None and setup in entry.setups

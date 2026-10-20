from __future__ import annotations

from dataclasses import dataclass, field

from tradesys.core.enums import SetupType
from tradesys.core.errors import ConfigError
from tradesys.core.identity import validate_client_id


CAPITAL_TRACKS: dict[str, float] = {
    "capital_preservation": 95.0,
    "balanced": 90.0,
    "growth": 80.0,
}
"""Named capital-floor presets an operator can set in YAML. Value = floor %."""


@dataclass(frozen=True, slots=True)
class RiskLevel:
    """One notch on the client's risk meter. Moves every loss limit together.

    A meter that moved only the floor would change nothing while a tighter
    drawdown halt binds first, so each level sets all four limits coherently:
    the drawdown halt sits exactly at the floor's distance from the base.
    """

    level: int
    name: str
    capital_floor_pct: float
    risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_drawdown_pct: float

    @property
    def beyond_protection(self) -> bool:
        return self.level > PROTECTED_RISK_LEVEL


PROTECTED_RISK_LEVEL = 3
RISK_LEVELS: dict[int, RiskLevel] = {
    lvl.level: lvl
    for lvl in (
        RiskLevel(1, "שמרני", 95.0, 0.25, 1.0, 5.0),
        RiskLevel(2, "מאוזן", 90.0, 0.5, 1.5, 10.0),
        RiskLevel(3, "צמיחה", 80.0, 0.75, 2.5, 20.0),
        RiskLevel(4, "אגרסיבי", 70.0, 1.0, 3.0, 30.0),
        RiskLevel(5, "ספקולטיבי", 60.0, 1.5, 4.0, 40.0),
    )
}
TRACK_LEVELS = {"capital_preservation": 1, "balanced": 2, "growth": 3}
"""Choices saved before the meter existed, read as the matching level."""


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

    # --- capital floor ------------------------------------------------------
    # Equity the book should not fall below, as % of the capital base. Entries
    # are sized so that even if every open stop is hit at once, equity stays
    # above the floor. A gap past a stop can still breach it: this is a brake,
    # not a guarantee. None disables the floor.
    capital_floor_pct: float | None = None
    # Capital the floor is measured against. Set when a client chooses a track,
    # to their equity at that moment: measuring from an older, higher balance
    # would put an account that is already down straight under its floor and
    # lock it out of trading for good. None falls back to the first equity the
    # journal recorded.
    capital_floor_base: float | None = None
    # Largest share of the remaining cushion (equity − floor − open risk) that
    # a single new trade may put at risk. 20% means five consecutive full-size
    # losses, not one, to reach the floor.
    floor_cushion_per_trade_pct: float = 20.0
    # Floor follows the equity peak instead of the starting capital, so gains
    # are protected as well as the deposit.
    floor_ratchet: bool = False

    # --- trailing stop ------------------------------------------------------
    # Distances are in R, the per-share risk taken at entry. Once a long is
    # `trail_activation_r` in profit its stop moves to breakeven, then follows
    # the peak at `trail_distance_r` below it. The stop never moves down.
    trailing_stop: bool = False
    trail_activation_r: float = 1.0
    trail_distance_r: float = 1.0
    # At the target, keep holding while the trend still confirms instead of
    # selling, locking in at least half the target gain. Needs the trail to exit.
    let_winners_run: bool = False

    # --- offense ------------------------------------------------------------
    # Rank each cycle's ideas by this tenant's proven edge and size them at
    # 100% / 75% / 50% of the per-trade ceiling (proven / unproven / losing
    # setup). Never above the ceiling: the Risk Manager clamps it.
    conviction_sizing: bool = False

    # --- client control ----------------------------------------------------
    # Most money the robot may have invested at once; None means the whole
    # account. Sizing is measured against min(equity, cap), and a new entry is
    # trimmed so open positions never exceed it.
    allocation_cap: float | None = None

    # --- diversification ----------------------------------------------------
    # Most of the robot's capital allowed in one sector at once, in percent.
    # Per-trade stops cannot help when a whole sector falls on the same day.
    max_sector_pct: float | None = None
    # A new name moving this closely with one already held is the same bet
    # twice; at or above this daily-return correlation it is refused.
    max_correlation: float | None = None
    # Autopilot: the robot sets its own per-trade risk from the market regime,
    # bolder in a rising market and smaller in a weak one, inside fixed limits.
    auto_pilot: bool = False

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
        if self.capital_floor_pct is not None and not 0 < self.capital_floor_pct < 100:
            raise ConfigError("capital_floor_pct must be between 0 and 100 (exclusive)")
        if self.capital_floor_base is not None and self.capital_floor_base <= 0:
            raise ConfigError("capital_floor_base must be positive when set")
        if not 0 < self.floor_cushion_per_trade_pct <= 100:
            raise ConfigError("floor_cushion_per_trade_pct must be in (0, 100]")
        if self.trail_activation_r <= 0 or self.trail_distance_r <= 0:
            raise ConfigError("trail_activation_r and trail_distance_r must be positive")
        if self.max_sector_pct is not None and not 0 < self.max_sector_pct <= 100:
            raise ConfigError("max_sector_pct must be in (0, 100] when set")
        if self.max_correlation is not None and not 0 < self.max_correlation <= 1:
            raise ConfigError("max_correlation must be in (0, 1] when set")
        if self.allocation_cap is not None and self.allocation_cap <= 0:
            raise ConfigError("allocation_cap must be positive when set")
        if self.let_winners_run and not self.trailing_stop:
            raise ConfigError(
                "let_winners_run needs trailing_stop: without it a position held past "
                "its target has no exit that protects the gain"
            )


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
    # Highest risk-meter level the operator lets this client choose. Levels above
    # PROTECTED_RISK_LEVEL go beyond capital protection and need explicit consent.
    max_risk_level: int = PROTECTED_RISK_LEVEL
    # When positive, the LeaderHunter picks this many of the strongest watchlist
    # symbols each week and the Scout looks only at them. The watchlist is then
    # the approved universe, not a list to trade all of.
    leaders_count: int = 0
    # Stop distance below entry in ATRs. Wider stops survive the noise of a long
    # trend; tighter ones suit short swings. R, and so sizing, follows from it.
    stop_atr_multiple: float = 1.5
    # Stocks the client picked; the Scout looks at them every cycle, on top of
    # whatever the LeaderHunter chose. Set from the tenant dir, never from YAML.
    pinned_symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_client_id(self.client_id)
        if self.leaders_count < 0:
            raise ConfigError("leaders_count must be zero (off) or positive")
        if self.stop_atr_multiple <= 0:
            raise ConfigError("stop_atr_multiple must be positive")
        if self.max_risk_level not in RISK_LEVELS:
            raise ConfigError(f"max_risk_level must be one of {sorted(RISK_LEVELS)}")
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

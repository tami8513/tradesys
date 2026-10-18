"""A client's chosen risk-meter level, stored per tenant.

The choice lives in the tenant's data directory rather than the profile YAML: the
YAML is operator-owned and checked into git, while the level is the client's own
decision, made in the UI. The operator still bounds it: `max_risk_level` in the
YAML caps what any choice can apply, and a level beyond capital protection takes
effect only with the client's recorded consent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from tradesys.clients.profile import (
    PROTECTED_RISK_LEVEL,
    RISK_LEVELS,
    TRACK_LEVELS,
    ClientProfile,
    RiskLevel,
)
from tradesys.core.errors import ConfigError
from tradesys.core.fileio import write_json_atomic

TRACK_FILE = "capital_track.json"


@dataclass(frozen=True, slots=True)
class TrackChoice:
    level: int
    base: float        # equity when the level was chosen; the floor is % of this
    chosen_at: str
    acknowledged: bool = False   # consent recorded for a level beyond protection


def read_track(tenant_dir: Path) -> TrackChoice | None:
    path = Path(tenant_dir) / TRACK_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        level = int(raw["level"]) if "level" in raw else TRACK_LEVELS[str(raw["track"])]
        choice = TrackChoice(
            level=level,
            base=float(raw["base"]),
            chosen_at=str(raw.get("chosen_at", "")),
            acknowledged=bool(raw.get("acknowledged", False)),
        )
    except (ValueError, OSError, KeyError, TypeError):
        return None
    if choice.level not in RISK_LEVELS or choice.base <= 0:
        return None
    return choice


def write_track(
    tenant_dir: Path,
    level: int | None,
    base_equity: float = 0.0,
    *,
    acknowledged: bool = False,
    max_level: int = PROTECTED_RISK_LEVEL,
) -> None:
    """Persist the choice with the equity it protects; None clears it."""
    path = Path(tenant_dir) / TRACK_FILE
    if level is None:
        if path.exists():
            path.unlink()
        return
    if level not in RISK_LEVELS:
        raise ConfigError(f"unknown risk level {level!r}")
    if level > max_level:
        raise ConfigError(f"risk level {level} is above this client's permitted {max_level}")
    if RISK_LEVELS[level].beyond_protection and not acknowledged:
        raise ConfigError("a level beyond capital protection needs the client's consent")
    if base_equity <= 0:
        raise ConfigError("a risk level needs the current equity as its base")
    write_json_atomic(
        path,
        {
            "level": level,
            "base": base_equity,
            "chosen_at": datetime.now(timezone.utc).isoformat(),
            "acknowledged": acknowledged,
        },
    )


def effective_level(profile: ClientProfile, choice: TrackChoice | None) -> RiskLevel | None:
    """The level that actually applies: never above the operator's cap, and never
    beyond protection without consent, whatever the file says."""
    if choice is None:
        return None
    level = min(choice.level, profile.max_risk_level)
    if RISK_LEVELS[level].beyond_protection and not choice.acknowledged:
        level = PROTECTED_RISK_LEVEL
    return RISK_LEVELS[level]


def apply_track(profile: ClientProfile, choice: TrackChoice | None) -> ClientProfile:
    level = effective_level(profile, choice)
    if level is None:
        return profile
    return replace(
        profile,
        risk=replace(
            profile.risk,
            capital_floor_pct=level.capital_floor_pct,
            capital_floor_base=choice.base,
            risk_per_trade_pct=level.risk_per_trade_pct,
            max_daily_loss_pct=level.max_daily_loss_pct,
            max_drawdown_pct=level.max_drawdown_pct,
        ),
    )

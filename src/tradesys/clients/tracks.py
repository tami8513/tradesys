"""A client's chosen capital-protection track, stored per tenant.

The choice lives in the tenant's data directory rather than the profile YAML: the
YAML is operator-owned and checked into git, while the track is the client's own
decision, made in the UI. The override can only set the floor; every other risk
limit stays exactly as the operator configured it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from tradesys.clients.profile import CAPITAL_TRACKS, ClientProfile
from tradesys.core.errors import ConfigError

TRACK_FILE = "capital_track.json"


@dataclass(frozen=True, slots=True)
class TrackChoice:
    track: str
    base: float        # equity when the track was chosen; the floor is % of this
    chosen_at: str


def read_track(tenant_dir: Path) -> TrackChoice | None:
    path = Path(tenant_dir) / TRACK_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        choice = TrackChoice(
            track=str(raw["track"]),
            base=float(raw["base"]),
            chosen_at=str(raw.get("chosen_at", "")),
        )
    except (ValueError, OSError, KeyError, TypeError):
        return None
    if choice.track not in CAPITAL_TRACKS or choice.base <= 0:
        return None
    return choice


def write_track(tenant_dir: Path, track: str | None, base_equity: float = 0.0) -> None:
    """Persist the choice with the equity it protects; None clears it."""
    path = Path(tenant_dir) / TRACK_FILE
    if track is None:
        if path.exists():
            path.unlink()
        return
    if track not in CAPITAL_TRACKS:
        raise ConfigError(f"unknown capital track {track!r}")
    if base_equity <= 0:
        raise ConfigError("a capital track needs the current equity as its base")
    path.write_text(
        json.dumps(
            {
                "track": track,
                "base": base_equity,
                "chosen_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def apply_track(profile: ClientProfile, choice: TrackChoice | None) -> ClientProfile:
    if choice is None:
        return profile
    return replace(
        profile,
        risk=replace(
            profile.risk,
            capital_floor_pct=CAPITAL_TRACKS[choice.track],
            capital_floor_base=choice.base,
        ),
    )


def track_of(profile: ClientProfile) -> str | None:
    """Which named track the profile's floor corresponds to, if any."""
    for name, pct in CAPITAL_TRACKS.items():
        if profile.risk.capital_floor_pct == pct:
            return name
    return None

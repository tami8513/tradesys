from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from tradesys.clients.profile import (
    CAPITAL_TRACKS,
    PROTECTED_RISK_LEVEL,
    BrokerConfig,
    ClientProfile,
    RiskProfile,
    WatchlistEntry,
)
from tradesys.core.enums import SetupType
from tradesys.core.errors import ConfigError
from tradesys.core.identity import validate_client_id

_SUPPORTED = (".yaml", ".yml", ".json")


def _read_document(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ConfigError(
            "PyYAML is required to read .yaml client profiles (pip install pyyaml), "
            "or store the profile as .json"
        ) from exc
    return yaml.safe_load(text)


def _require(doc: Mapping[str, Any], key: str, path: Path) -> Any:
    if key not in doc:
        raise ConfigError(f"{path.name}: missing required key {key!r}")
    return doc[key]


def _setups(values: Any, path: Path) -> frozenset[SetupType]:
    try:
        return frozenset(SetupType(v) for v in values or ())
    except ValueError as exc:
        raise ConfigError(f"{path.name}: unknown setup type ({exc})") from exc


def _capital_floor(raw_risk: Mapping[str, Any], path: Path) -> float | None:
    """An explicit capital_floor_pct wins; otherwise a named capital_track."""
    if raw_risk.get("capital_floor_pct") is not None:
        return float(raw_risk["capital_floor_pct"])
    track = raw_risk.get("capital_track")
    if track is None:
        return None
    if track not in CAPITAL_TRACKS:
        raise ConfigError(
            f"{path.name}: unknown capital_track {track!r} "
            f"(expected one of {sorted(CAPITAL_TRACKS)})"
        )
    return CAPITAL_TRACKS[track]


def parse_profile(doc: Mapping[str, Any], path: Path) -> ClientProfile:
    """Validate an untrusted config document into a ClientProfile."""
    if not isinstance(doc, Mapping):
        raise ConfigError(f"{path.name}: expected a mapping at the document root")

    client_id = validate_client_id(str(_require(doc, "client_id", path)))

    raw_risk = _require(doc, "risk", path)
    try:
        risk = RiskProfile(
            max_drawdown_pct=float(_require(raw_risk, "max_drawdown_pct", path)),
            max_daily_loss_pct=float(_require(raw_risk, "max_daily_loss_pct", path)),
            risk_per_trade_pct=float(_require(raw_risk, "risk_per_trade_pct", path)),
            max_position_pct=float(_require(raw_risk, "max_position_pct", path)),
            max_open_positions=int(_require(raw_risk, "max_open_positions", path)),
            max_trades_per_day=int(_require(raw_risk, "max_trades_per_day", path)),
            min_reward_risk=float(_require(raw_risk, "min_reward_risk", path)),
            allow_short=bool(raw_risk.get("allow_short", False)),
            allow_leverage=bool(raw_risk.get("allow_leverage", False)),
            forbidden_setups=_setups(raw_risk.get("forbidden_setups"), path),
            min_price=float(raw_risk.get("min_price", 1.0)),
            max_position_pct_of_adv=float(raw_risk.get("max_position_pct_of_adv", 0.02)),
            avoid_earnings_within_days=(
                int(raw_risk["avoid_earnings_within_days"])
                if raw_risk.get("avoid_earnings_within_days") is not None
                else None
            ),
            regime_symbol=str(raw_risk.get("regime_symbol", "SPY")),
            regime_sma_period=int(raw_risk.get("regime_sma_period", 200)),
            regime_blocked_setups=_setups(raw_risk.get("regime_blocked_setups"), path),
            capital_floor_pct=_capital_floor(raw_risk, path),
            floor_cushion_per_trade_pct=float(
                raw_risk.get("floor_cushion_per_trade_pct", 20.0)
            ),
            floor_ratchet=bool(raw_risk.get("floor_ratchet", False)),
            trailing_stop=bool(raw_risk.get("trailing_stop", False)),
            trail_activation_r=float(raw_risk.get("trail_activation_r", 1.0)),
            trail_distance_r=float(raw_risk.get("trail_distance_r", 1.0)),
            let_winners_run=bool(raw_risk.get("let_winners_run", False)),
            conviction_sizing=bool(raw_risk.get("conviction_sizing", False)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path.name}: invalid risk block ({exc})") from exc

    watchlist = tuple(
        WatchlistEntry(
            symbol=str(_require(item, "symbol", path)),
            setups=_setups(item.get("setups"), path),
            notes=str(item.get("notes", "")),
            max_position_pct=(
                float(item["max_position_pct"])
                if item.get("max_position_pct") is not None
                else None
            ),
        )
        for item in _require(doc, "watchlist", path)
    )

    raw_broker = doc.get("broker", {})
    broker = BrokerConfig(
        name=str(raw_broker.get("name", "paper")),
        paper=bool(raw_broker.get("paper", True)),
        credential_ref=str(raw_broker.get("credential_ref", "")),
        base_url=raw_broker.get("base_url"),
    )

    return ClientProfile(
        client_id=client_id,
        display_name=str(doc.get("display_name", client_id)),
        watchlist=watchlist,
        risk=risk,
        broker=broker,
        timeframe=str(doc.get("timeframe", "1Day")),
        lookback_bars=int(doc.get("lookback_bars", 120)),
        max_candidates_per_cycle=int(doc.get("max_candidates_per_cycle", 5)),
        memory_top_k=int(doc.get("memory_top_k", 5)),
        monitor_interval_seconds=float(doc.get("monitor_interval_seconds", 60.0)),
        max_holding_days=(
            int(doc["max_holding_days"]) if doc.get("max_holding_days") is not None else None
        ),
        tags=tuple(str(t) for t in doc.get("tags", ())),
        metadata={str(k): str(v) for k, v in (doc.get("metadata") or {}).items()},
        max_risk_level=int(doc.get("max_risk_level", PROTECTED_RISK_LEVEL)),
    )


class ClientRegistry:
    """Loads and caches tenant profiles from a configuration directory."""

    def __init__(self, clients_dir: Path) -> None:
        self._dir = Path(clients_dir)
        self._cache: dict[str, ClientProfile] = {}

    def _path_for(self, client_id: str) -> Path:
        validate_client_id(client_id)
        for suffix in _SUPPORTED:
            candidate = self._dir / f"{client_id}{suffix}"
            if candidate.is_file():
                return candidate
        raise ConfigError(f"no profile found for client_id={client_id!r} in {self._dir}")

    def load(self, client_id: str, *, refresh: bool = False) -> ClientProfile:
        if refresh:
            self._cache.pop(client_id, None)
        if client_id not in self._cache:
            path = self._path_for(client_id)
            profile = parse_profile(_read_document(path), path)
            if profile.client_id != client_id:
                raise ConfigError(
                    f"{path.name} declares client_id={profile.client_id!r} "
                    f"but is filed under {client_id!r}"
                )
            self._cache[client_id] = profile
        return self._cache[client_id]

    def list_ids(self) -> list[str]:
        seen = {
            p.stem for p in self._dir.glob("*") if p.suffix in _SUPPORTED and p.is_file()
        }
        return sorted(seen)

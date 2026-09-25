from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tradesys.agents.execution import ExecutionAgent, ExecutionOutcome
from tradesys.agents.monitor import MonitorReport, PositionMonitor
from tradesys.agents.reflection import ReflectionAgent
from tradesys.agents.risk_manager import RiskDecision, RiskManager
from tradesys.agents.scout import ScoutAgent
from tradesys.brokers.base import Broker
from tradesys.brokers.paper import PaperBroker
from tradesys.clients.loader import ClientRegistry
from tradesys.clients.profile import ClientProfile
from tradesys.clients.secrets import EnvSecretResolver, SecretResolver
from tradesys.core.clock import Clock, SystemClock, trading_day
from tradesys.core.config import Settings
from tradesys.core.errors import ConfigError
from tradesys.core.logging import configure, get_logger
from tradesys.core.tenancy import TenantContext
from tradesys.data.earnings import (
    EarningsCalendar,
    FinnhubEarningsCalendar,
    NullEarningsCalendar,
    StaticEarningsCalendar,
)
from tradesys.data.market_data import MarketDataProvider, SyntheticMarketData
from tradesys.data.regime import MarketRegime, assess_regime
from tradesys.memory.directives import DirectiveExtractor
from tradesys.memory.store import LocalVectorMemory, VectorMemory
from tradesys.persistence.journal import Journal


@dataclass(slots=True)
class CycleReport:
    client_id: str
    started_at: datetime
    halted: bool = False
    halt_reason: str = ""
    candidates: int = 0
    approvals: int = 0
    submissions: int = 0
    rejections: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    outcomes: list[ExecutionOutcome] = field(default_factory=list)
    monitor: MonitorReport | None = None
    regime: MarketRegime | None = None

    def summary(self) -> str:
        exits = len(self.monitor.closed) if self.monitor else 0
        prefix = f"[{self.client_id}] {exits} exit(s)"
        if self.halted:
            return f"{prefix} | HALTED — {self.halt_reason}"
        return (
            f"{prefix} | {self.candidates} candidate(s), "
            f"{self.approvals} approved, {self.submissions} submitted"
        )


class TradingSystem:
    """Builds a fully wired, tenant-isolated agent stack for one client_id.

    Every dependency is constructed *inside* the tenant context, so an agent
    physically cannot reach another tenant's journal, memory or broker keys. Two
    tenants running in the same process share nothing but the Settings object.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        registry: ClientRegistry | None = None,
        secrets: SecretResolver | None = None,
        market_data: MarketDataProvider | None = None,
        clock: Clock | None = None,
        extractor: DirectiveExtractor | None = None,
        earnings: EarningsCalendar | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        configure(self.settings.log_level)
        self.registry = registry or ClientRegistry(self.settings.clients_dir)
        self.secrets = secrets or EnvSecretResolver()
        self.clock = clock or SystemClock()
        self._market_data_override = market_data
        self._extractor = extractor
        self._earnings_override = earnings

    # ------------------------------------------------------------- factories

    def tenant(self, client_id: str) -> TenantContext:
        return TenantContext.create(client_id, self.settings.data_dir)

    def _memory(self, tenant: TenantContext) -> VectorMemory:
        if self.settings.memory_backend == "chroma":
            from tradesys.memory.chroma_store import ChromaVectorMemory

            return ChromaVectorMemory(tenant)
        return LocalVectorMemory(tenant)

    def _earnings(self) -> EarningsCalendar:
        """Vendor feed when a key is configured, otherwise the operator's file.

        Shared across tenants: an earnings date is a fact about the market, not
        about a client, so there is nothing tenant-specific to isolate here.
        """
        if self._earnings_override is not None:
            return self._earnings_override
        if self.settings.finnhub_api_key:
            return FinnhubEarningsCalendar(self.settings.finnhub_api_key)
        if self.settings.earnings_file is None:
            return NullEarningsCalendar()
        return StaticEarningsCalendar(self.settings.earnings_file)

    def _market_data(self, profile: ClientProfile) -> MarketDataProvider:
        """Where a tenant's prices come from.

        Paper tenants keep their balance in our own PaperBroker ledger either way;
        what changes with platform data keys is only the price feed. That is the
        demo a prospect should see: real market moves, simulated money.
        """
        if self._market_data_override is not None:
            return self._market_data_override

        from tradesys.data.market_data import AlpacaMarketData

        if profile.broker.name == "paper":
            if self.settings.market_data_key_id and self.settings.market_data_secret_key:
                return AlpacaMarketData(
                    self.settings.market_data_key_id,
                    self.settings.market_data_secret_key,
                    feed=self.settings.market_data_feed,
                )
            return SyntheticMarketData()

        credentials = self.secrets.broker_credentials(profile)
        return AlpacaMarketData(
            credentials.key_id, credentials.secret_key, feed=self.settings.market_data_feed
        )

    def market_data_source(self) -> str:
        """Human-readable label of the paper-tenant price feed, for the UI."""
        if self._market_data_override is not None:
            return type(self._market_data_override).__name__
        if self.settings.market_data_key_id and self.settings.market_data_secret_key:
            return f"alpaca:{self.settings.market_data_feed}"
        return "synthetic"

    def _broker(
        self,
        tenant: TenantContext,
        profile: ClientProfile,
        market_data: MarketDataProvider,
    ) -> Broker:
        if profile.broker.name == "paper":
            return PaperBroker(tenant, market_data)
        if profile.broker.name == "alpaca":
            from tradesys.brokers.alpaca import AlpacaBroker

            return AlpacaBroker(tenant, self.secrets.broker_credentials(profile))
        raise ConfigError(f"unknown broker {profile.broker.name!r}")

    def for_client(self, client_id: str) -> "ClientSession":
        profile = self.registry.load(client_id)
        tenant = self.tenant(client_id)
        journal = Journal(tenant)
        memory = self._memory(tenant)
        market_data = self._market_data(profile)
        broker = self._broker(tenant, profile, market_data)

        reflection = ReflectionAgent(
            tenant, profile, memory, journal, self._extractor, self.clock
        )
        execution = ExecutionAgent(
            tenant,
            profile,
            broker,
            journal,
            allow_live_trading=self.settings.allow_live_trading,
            clock=self.clock,
        )
        return ClientSession(
            tenant=tenant,
            profile=profile,
            journal=journal,
            memory=memory,
            scout=ScoutAgent(
                tenant,
                profile,
                market_data,
                memory,
                reflection.active_directives(),
                self.clock,
            ),
            risk=RiskManager(
                tenant, profile, journal, self.clock, earnings=self._earnings()
            ),
            execution=execution,
            reflection=reflection,
            monitor=PositionMonitor(
                tenant,
                profile,
                market_data,
                broker,
                journal,
                execution,
                reflection,
                self.clock,
                max_holding_days=profile.max_holding_days,
            ),
            broker=broker,
            market_data=market_data,
            clock=self.clock,
        )


@dataclass(slots=True)
class ClientSession:
    """One tenant's live agent stack, plus the cycle that sequences them."""

    tenant: TenantContext
    profile: ClientProfile
    journal: Journal
    memory: VectorMemory
    scout: ScoutAgent
    risk: RiskManager
    execution: ExecutionAgent
    reflection: ReflectionAgent
    monitor: PositionMonitor
    broker: Broker
    market_data: MarketDataProvider
    clock: Clock

    @property
    def client_id(self) -> str:
        return self.tenant.client_id

    def market_regime(self) -> MarketRegime | None:
        """Current broad-market read, or None when the filter is off/unreadable."""
        limits = self.profile.risk
        if not limits.regime_blocked_setups:
            return None
        return assess_regime(
            self.market_data,
            limits.regime_symbol,
            period=limits.regime_sma_period,
            timeframe=self.profile.timeframe,
            now=self.clock.now(),
        )

    def run_cycle(self) -> CycleReport:
        """Monitor -> Scout -> Risk -> Execute -> Reflect, for this tenant only."""
        log = get_logger("tradesys.engine", self.client_id)
        report = CycleReport(client_id=self.client_id, started_at=self.clock.now())

        # Manage existing risk before taking on any more: an exit here frees a
        # position slot and books P&L that the risk checks below will see.
        report.monitor = self.monitor.check_once()

        account = self.broker.account()
        preflight = self.risk.preflight(account)
        if not preflight.ok:
            report.halted = True
            report.halt_reason = preflight.reason
            log.warning("cycle aborted: %s", preflight.reason)
            return report

        positions = self.broker.positions()
        stats = self.journal.day_stats(trading_day(self.clock))

        # Read once per cycle, not per candidate: it is one fact about the market
        # and re-reading it mid-loop could gate two candidates differently.
        regime = self.market_regime()
        report.regime = regime

        candidates = self.scout.scan()
        report.candidates = len(candidates)

        for candidate in candidates:
            decision: RiskDecision = self.risk.evaluate(
                candidate, account, positions, stats, regime
            )
            if not decision.approved:
                report.rejections.append((candidate.symbol, decision.reasons))
                continue

            report.approvals += 1
            outcome = self.execution.execute(decision)
            report.outcomes.append(outcome)
            if outcome.submitted:
                report.submissions += 1
                # Re-read state so later candidates in this same cycle are evaluated
                # against the exposure the earlier ones just created.
                account = self.broker.account()
                positions = self.broker.positions()
                stats = self.journal.day_stats(trading_day(self.clock))

        self.reflection.reflect()
        log.info(report.summary())
        return report

    def close(self) -> None:
        self.journal.close()

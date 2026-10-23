from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Sequence

from tradesys.agents.execution import ExecutionAgent, ExecutionOutcome
from tradesys.agents.hunter import LeaderHunter
from tradesys.agents.monitor import MonitorReport, PositionMonitor
from tradesys.agents.offense import OffenseAgent
from tradesys.agents.reflection import ReflectionAgent
from tradesys.agents.risk_manager import RiskDecision, RiskManager
from tradesys.agents.scout import ScoutAgent
from tradesys.brokers.base import Broker
from tradesys.brokers.paper import PaperBroker
from tradesys.clients.loader import ClientRegistry
from tradesys.clients.profile import ClientProfile
from tradesys.clients.secrets import EnvSecretResolver, SecretResolver
from tradesys.clients.picks import PREAPPROVAL_MAX, apply_picks, read_picks
from tradesys.clients.proposals import propose, read_proposals, resolve
from tradesys.clients.tracks import apply_control, read_control, read_track
from tradesys.core.clock import Clock, SystemClock, trading_day
from tradesys.core.enums import SetupType, Side
from tradesys.core.config import Settings
from tradesys.core.errors import ConfigError
from tradesys.core.logging import configure, get_logger
from tradesys.core.models import Candidate, Position
from tradesys.data.indicators import return_correlation
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
from tradesys.orchestration.agent_report import REASON_SEP
from tradesys.orchestration.index_desk import book_account, reserved_symbols, run_index_robot
from tradesys.orchestration.morning import carry_out
from tradesys.orchestration.why import explain_reason
from tradesys.persistence.journal import Journal

AgentEvent = tuple[str, str, str, str]


def _within_budget(decision: RiskDecision, budget: float) -> RiskDecision:
    """Trim an approved entry to the client's standing dollar cap, never grow it."""
    cap = int(budget // decision.order.limit_price)
    if cap >= decision.qty:
        return decision
    if cap < 1:
        return replace(
            decision, approved=False, qty=0, risk_amount=0.0, order=None,
            reasons=(f"standing approval of ${budget:,.0f} buys less than one share",),
        )
    return replace(
        decision,
        qty=cap,
        risk_amount=decision.risk_amount * cap / decision.qty,
        order=replace(decision.order, qty=cap),
    )


def opening_noise(now: datetime) -> bool:
    """The first quarter hour of the session, when the robot opens nothing new.

    Quotes jump and spreads are wide right after the bell: every entry the
    robot sent at 9:30-9:31 went unfilled and was cancelled. Exits and stops
    are not affected; this only delays new entries.
    """
    from datetime import time
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo("America/New_York")).time()
    return time(9, 30) <= local < time(9, 30 + OPENING_QUIET_MINUTES)


def session_close(now: datetime) -> datetime:
    """16:00 New York on `now`'s trading day: when a proposal stops being valid."""
    from datetime import time
    from zoneinfo import ZoneInfo

    new_york = ZoneInfo("America/New_York")
    day = now.astimezone(new_york).date()
    return datetime.combine(day, time(16, 0), new_york)
CORRELATION_BARS = 61     # sixty daily returns
OPENING_QUIET_MINUTES = 15


def _monitor_events(report: MonitorReport) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    for closed in report.closed:
        events.append(
            (
                "monitor",
                "exit",
                closed.trade.symbol,
                f"{closed.reason.value}|{closed.trade.r_multiple():.2f}",
            )
        )
        if closed.lessons:
            events.append(("reflection", "lessons", closed.trade.symbol, str(len(closed.lessons))))
    for line in report.adjusted:
        kind = ("held_past_target" if "holding" in line
                else "target_restored" if "target restored" in line else "stop_raised")
        events.append(("monitor", kind, line.split(" ", 1)[0], line))
    events.extend(("monitor", "error", "", error) for error in report.errors)
    return events


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

    def client_profile(self, client_id: str) -> ClientProfile:
        """The profile as the client set it: the YAML, their risk meter and their picks.

        The client's risk-meter level sets the four loss limits, capped by the
        operator's max_risk_level; everything else stays as the YAML sets it.
        """
        data_dir = self.tenant(client_id).data_dir
        profile = apply_control(
            self.registry.load(client_id), read_control(data_dir), read_track(data_dir)
        )
        return apply_picks(profile, read_picks(data_dir))

    def for_client(self, client_id: str) -> "ClientSession":
        tenant = self.tenant(client_id)
        profile = self.client_profile(client_id)
        journal = Journal(tenant)
        memory = self._memory(tenant)
        market_data = self._market_data(profile)
        broker = self._broker(tenant, profile, market_data)

        earnings = self._earnings()
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
            risk=RiskManager(tenant, profile, journal, self.clock, earnings=earnings),
            offense=OffenseAgent(tenant, profile, journal, self.clock),
            hunter=LeaderHunter(tenant, profile, market_data, self.clock),
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
                earnings=earnings,
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
    offense: OffenseAgent
    hunter: LeaderHunter
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
        """Current broad-market read, or None when nothing uses it or it is unreadable."""
        limits = self.profile.risk
        # Autopilot sizes from the regime too; skipping the read there would
        # pin it to its weak-market setting forever.
        if not limits.regime_blocked_setups and not limits.auto_pilot:
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
        # The client's morning answers first: a profit they asked to take is taken
        # at the open, before the sweep could act on the same position.
        decided = carry_out(self)
        report.monitor = self.monitor.check_once()
        events = decided + _monitor_events(report.monitor)

        # The main robot's checks see the account without the index robot's
        # holdings moving, which have no stops and are not its to protect.
        account = book_account(self)
        preflight = self.risk.preflight(account)
        # Selling (insurance, a removed index) stays allowed while halted; buying does not.
        events.extend(run_index_robot(self, can_buy=preflight.ok))
        if not preflight.ok:
            report.halted = True
            report.halt_reason = preflight.reason
            log.warning("cycle aborted: %s", preflight.reason)
            events.append(("risk", "halted", "", preflight.reason))
            self.journal.log_events(events, report.started_at)
            return report

        if opening_noise(self.clock.now()):
            events.append(("scout", "opening_wait", "", ""))
            self.journal.log_events(events, report.started_at)
            return report

        positions = self.broker.positions()
        stats = self.journal.day_stats(trading_day(self.clock))

        # Read once per cycle, not per candidate: it is one fact about the market
        # and re-reading it mid-loop could gate two candidates differently.
        regime = self.market_regime()
        report.regime = regime

        focus = None
        if self.profile.leaders_count:
            leaders = self.hunter.leaders()
            focus = {leader.symbol for leader in leaders} | set(self.profile.pinned_symbols)
            events.append(
                (
                    "hunter",
                    "leaders",
                    "",
                    " ".join(f"{leader.symbol}:{leader.score:.1f}" for leader in leaders),
                )
            )
        reserved = reserved_symbols(self.tenant.data_dir)
        candidates = [c for c in self.scout.scan(focus) if c.symbol.upper() not in reserved]
        report.candidates = len(candidates)

        offense_on = self.profile.risk.conviction_sizing
        plan = self.offense.plan(candidates) if offense_on else [(c, 1.0) for c in candidates]
        # Offense ranks by edge; the client's priority picks still go first.
        priority = set(self.profile.priority_symbols)
        plan.sort(key=lambda item: item[0].symbol not in priority)

        for candidate, conviction in plan:
            events.append(
                (
                    "scout",
                    "candidate",
                    candidate.symbol,
                    f"{candidate.setup.value}|{candidate.rationale}",
                )
            )
            if offense_on:
                events.append(
                    ("offense", "sized", candidate.symbol, f"{conviction:.2f}|{candidate.setup.value}")
                )
            candidate = self._at_live_price(candidate)
            decision: RiskDecision = self.risk.evaluate(
                candidate,
                account,
                positions,
                stats,
                regime,
                conviction=conviction,
                twin=self._closest_twin(candidate.symbol, positions),
            )
            if not decision.approved:
                report.rejections.append((candidate.symbol, decision.reasons))
                events.append(
                    ("risk", "rejected", candidate.symbol, REASON_SEP.join(decision.reasons))
                )
                continue

            report.approvals += 1
            events.append(("risk", "approved", candidate.symbol, candidate.setup.value))
            standing = self._standing_approval(candidate.symbol)
            if standing is not None:
                decision = _within_budget(decision, standing)
                if not decision.approved:
                    events.append(
                        ("risk", "rejected", candidate.symbol, REASON_SEP.join(decision.reasons))
                    )
                    continue
                events.append(
                    ("client", "preapproved", candidate.symbol, str(decision.qty))
                )
            elif candidate.symbol in self.profile.approval_symbols:
                proposal = propose(
                    self.tenant.data_dir,
                    candidate,
                    decision.qty,
                    self.clock.now(),
                    session_close(self.clock.now()),
                )
                if proposal is not None:
                    events.append(
                        ("execution", "awaiting_approval", candidate.symbol, str(decision.qty))
                    )
                continue
            outcome = self.execution.execute(decision)
            report.outcomes.append(outcome)
            events.append(
                (
                    "execution",
                    outcome.result.status.value if outcome.result else "not_sent",
                    candidate.symbol,
                    outcome.reason,
                )
            )
            if outcome.submitted:
                report.submissions += 1
                # Re-read state so later candidates in this same cycle are evaluated
                # against the exposure the earlier ones just created.
                account = book_account(self)
                positions = self.broker.positions()
                stats = self.journal.day_stats(trading_day(self.clock))

        lessons = self.reflection.reflect()
        if lessons:
            events.append(("reflection", "lessons", "", str(len(lessons))))
        self.journal.log_events(events, report.started_at)
        log.info(report.summary())
        return report

    def execute_proposal(self, proposal_id: str) -> str:
        """Carry out a proposal the client approved, re-checking it from scratch.

        Returns a one-line Hebrew outcome for the screen. The idea is re-priced at
        the live trade and the Risk Manager runs again on today's book, so an
        approval can still be refused if the market or the book moved.
        """
        now = self.clock.now()
        proposal = next(
            (p for p in read_proposals(self.tenant.data_dir) if p.proposal_id == proposal_id),
            None,
        )
        if proposal is None or proposal.status != "pending":
            return "הבקשה כבר טופלה."
        if proposal.expired(now):
            resolve(self.tenant.data_dir, proposal_id, "expired", "תוקף הבקשה פג")
            return "תוקף הבקשה פג בסוף יום המסחר."

        candidate = self._at_live_price(
            Candidate(
                client_id=self.client_id,
                symbol=proposal.symbol,
                setup=SetupType(proposal.setup),
                side=Side.BUY,
                entry=proposal.entry,
                stop=proposal.stop,
                target=proposal.target,
                confidence=proposal.confidence,
                rationale=proposal.rationale,
                features={"avg_volume": proposal.avg_volume},
            )
        )
        account = book_account(self)
        positions = self.broker.positions()
        stats = self.journal.day_stats(trading_day(self.clock))
        decision = self.risk.evaluate(
            candidate,
            account,
            positions,
            stats,
            self.market_regime(),
            twin=self._closest_twin(candidate.symbol, positions),
        )
        if not decision.approved:
            note = "שומר הסיכון חסם: " + " · ".join(explain_reason(r) for r in decision.reasons)
            resolve(self.tenant.data_dir, proposal_id, "blocked", note)
            self.journal.log_events([("client", "approved", proposal.symbol, note)], now)
            return note
        if decision.qty > proposal.qty:
            # The client agreed to a size; a re-check may shrink it, never grow it.
            decision = replace(
                decision,
                qty=proposal.qty,
                risk_amount=decision.risk_amount * proposal.qty / decision.qty,
                order=replace(decision.order, qty=proposal.qty),
            )
        outcome = self.execution.execute(decision)
        if outcome.trade_id is not None:
            note = f"נקנו {outcome.result.filled_qty} מניות ב-{outcome.result.filled_avg_price:,.2f}"
            status = "approved"
        else:
            note = "הפקודה לא בוצעה: " + explain_reason(outcome.reason or "not filled")
            status = "blocked"
        resolve(self.tenant.data_dir, proposal_id, status, note)
        self.journal.log_events([("client", "approved", proposal.symbol, note)], now)
        return note

    def _standing_approval(self, symbol: str) -> float | None:
        """The dollar budget the client approved for `symbol` today, or None.

        Never above PREAPPROVAL_MAX, whatever the saved file says: an approval
        saved without an amount is not an open cheque.
        """
        today = trading_day(self.clock).isoformat()
        for approved_symbol, day, max_amount in self.profile.preapprovals:
            if approved_symbol == symbol and day == today:
                return min(max_amount or PREAPPROVAL_MAX, PREAPPROVAL_MAX)
        return None

    def decline_proposal(self, proposal_id: str) -> None:
        proposal = resolve(self.tenant.data_dir, proposal_id, "declined", "הלקוח דחה")
        if proposal is not None:
            self.journal.log_events(
                [("client", "declined", proposal.symbol, "")], self.clock.now()
            )

    def _closest_twin(
        self, symbol: str, positions: Sequence[Position]
    ) -> tuple[str, float] | None:
        """The held name whose daily returns track `symbol` most closely."""
        if self.profile.risk.max_correlation is None:
            return None
        held = [p.symbol for p in positions if p.symbol != symbol]
        if not held:
            return None
        try:
            mine = self.market_data.history(symbol, timeframe="1Day", limit=CORRELATION_BARS)
            pairs = [
                (other, return_correlation(
                    mine,
                    self.market_data.history(other, timeframe="1Day", limit=CORRELATION_BARS),
                ))
                for other in held
            ]
        except Exception:  # unmeasurable means unknown, and the sector cap still applies
            return None
        measured = [(other, c) for other, c in pairs if c is not None]
        return max(measured, key=lambda pair: pair[1]) if measured else None

    def _at_live_price(self, candidate: Candidate) -> Candidate:
        """Re-price an idea at the live trade before the Risk Manager sizes it.

        The Scout reads daily bars, and on the free data plan the full-tape bars
        trail the market by 15 minutes. A limit at that stale close sat below a
        rising price and was cancelled unfilled. The stop and target stay where
        the setup put them, so the Risk Manager re-checks reward:risk and size at
        the price actually paid, and refuses an idea the market has run away from.
        """
        try:
            live = self.market_data.last_price(candidate.symbol)
        except Exception:  # without a live print the original idea stands
            return candidate
        if live <= 0:
            return candidate
        return replace(candidate, entry=round(live, 4))

    def close(self) -> None:
        self.journal.close()

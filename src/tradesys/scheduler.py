"""Runs trading cycles by itself, in step with the exchange's opening hours.

    python -m tradesys.scheduler

While the market is open, every TRADESYS_CYCLE_MINUTES (default 15) it runs one
full cycle — monitor sweep, scout, risk, execution, reflection — for each tenant
whose automation switch is on and whose account is not suspended. While closed,
it sleeps until the next open, re-checking at least every half hour.

Failures are contained per tenant and per loop: one tenant's broken broker or a
data outage never stops the others, and never stops the loop.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from tradesys.clients.automation import automation_enabled, lifecycle_active
from tradesys.clients.picks import approval_day
from tradesys.core.config import Settings
from tradesys.core.logging import configure
from tradesys.data.market_clock import MarketClock, market_clock
from tradesys.orchestration.engine import TradingSystem
from tradesys.orchestration.scouting import scout_due, scout_market
from tradesys.orchestration.watchdog import (
    AlertBook,
    Notifier,
    cycle_failure,
    greet_once,
    inspect,
    notifier_from_env,
)

log = logging.getLogger("tradesys.scheduler")

IDLE_RECHECK_SECONDS = 30 * 60


def due_tenants(system: TradingSystem) -> list[str]:
    due = []
    for client_id in system.registry.list_ids():
        tenant_dir = system.tenant(client_id).data_dir
        if automation_enabled(tenant_dir, client_id) and lifecycle_active(tenant_dir):
            due.append(client_id)
    return due


def run_once(system: TradingSystem) -> dict[str, str]:
    """One cycle for every due tenant. Returns client_id -> one-line outcome."""
    outcomes: dict[str, str] = {}
    for client_id in due_tenants(system):
        session = None
        try:
            session = system.for_client(client_id)
            report = session.run_cycle()
            outcomes[client_id] = report.summary()
            log.info(report.summary())
        except Exception as exc:
            outcomes[client_id] = f"failed: {type(exc).__name__}: {exc}"
            log.exception("cycle failed for %s", client_id)
        finally:
            if session is not None:
                session.close()
    return outcomes


def watch(
    system: TradingSystem,
    book: AlertBook,
    notifier: Notifier,
    outcomes: dict[str, str] | None = None,
) -> None:
    """Inspect every due tenant and send what is new, still open, or resolved."""
    now = datetime.now(timezone.utc)
    for client_id in due_tenants(system):
        session = None
        try:
            session = system.for_client(client_id)
            alerts = inspect(session)
            failure = cycle_failure(client_id, (outcomes or {}).get(client_id, ""))
            if failure is not None:
                alerts.append(failure)
            for message in book.update(client_id, alerts, now):
                notifier.send(message)
        except Exception:
            # The watchdog must never take the trading loop down with it.
            log.exception("watchdog failed for %s", client_id)
        finally:
            if session is not None:
                session.close()


def seconds_until(moment: datetime, now: datetime) -> float:
    return max(0.0, (moment - now).total_seconds())


def scout_overnight(settings: Settings) -> None:
    """Write the talent scout's report for the coming trading day, once."""
    day = approval_day(datetime.now(timezone.utc))
    if not scout_due(settings, day):
        return
    try:
        scout_market(settings, day)
    except Exception:
        log.exception("talent scout failed; retrying on the next idle check")


def run_forever(
    system: TradingSystem,
    clock: MarketClock,
    cycle_minutes: float,
    notifier: Notifier | None = None,
) -> None:
    log.info("scheduler started: cycle every %.0f min while the market is open", cycle_minutes)
    book = AlertBook(system.settings.data_dir / "_platform" / "alerts.json")
    notifier = notifier or notifier_from_env()
    try:
        greet_once(notifier, system.settings.data_dir / "_platform" / "telegram_greeted.json")
    except Exception:  # a phone channel being down must never stop trading
        log.exception("could not send the alerts greeting")
    while True:
        try:
            status = clock.status()
            if status.is_open:
                watch(system, book, notifier, run_once(system))
                time.sleep(cycle_minutes * 60)
                continue
            watch(system, book, notifier)
            scout_overnight(system.settings)
            wait = seconds_until(status.next_open, datetime.now(timezone.utc))
            log.info(
                "market closed (%s); next open %s; sleeping %.0f min",
                status.source,
                status.next_open.isoformat(),
                min(wait, IDLE_RECHECK_SECONDS) / 60,
            )
            time.sleep(max(60.0, min(wait, IDLE_RECHECK_SECONDS)))
        except Exception:
            log.exception("scheduler loop error; retrying in 60s")
            time.sleep(60)


def _secret_resolver(settings: Settings):
    """Same resolution as the dashboard: encrypted vault first, then env vars.

    The vault module lives at the repo root next to app.py; when the scheduler
    runs from there it is importable, otherwise env-var credentials still work.
    """
    try:
        import accounts
    except ImportError:
        return None
    return accounts.VaultSecretResolver(settings.data_dir)


def main() -> None:
    settings = Settings.from_env()
    configure(settings.log_level)
    system = TradingSystem(settings, secrets=_secret_resolver(settings))
    clock = market_clock(settings.market_data_key_id, settings.market_data_secret_key)
    run_forever(system, clock, float(os.getenv("TRADESYS_CYCLE_MINUTES", "15")))


if __name__ == "__main__":
    main()

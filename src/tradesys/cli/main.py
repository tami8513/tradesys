from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tradesys.core.config import Settings
from tradesys.core.errors import TradesysError
from tradesys.orchestration.engine import TradingSystem


def _system(args: argparse.Namespace) -> TradingSystem:
    settings = Settings.from_env(Path(args.root) if args.root else None)
    return TradingSystem(settings)


def cmd_clients(args: argparse.Namespace) -> int:
    system = _system(args)
    for client_id in system.registry.list_ids():
        profile = system.registry.load(client_id)
        print(
            f"{client_id:<16} {profile.display_name:<24} "
            f"{len(profile.watchlist)} symbols  "
            f"broker={profile.broker.name}({'paper' if profile.broker.paper else 'LIVE'})  "
            f"maxDD={profile.risk.max_drawdown_pct}%"
        )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    session = _system(args).for_client(args.client)
    try:
        report = session.run_cycle()
        print(report.summary())
        for symbol, reasons in report.rejections:
            print(f"  rejected {symbol}: {'; '.join(reasons)}")
        for outcome in report.outcomes:
            status = "submitted" if outcome.submitted else f"skipped ({outcome.reason})"
            trade = f" trade #{outcome.trade_id}" if outcome.trade_id else ""
            print(f"  {outcome.symbol}: {status}{trade}")
    finally:
        session.close()
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """Run the exit manager as its own process."""
    session = _system(args).for_client(args.client)
    interval = args.interval or session.profile.monitor_interval_seconds
    try:
        print(f"monitoring {session.client_id} every {interval:.0f}s (ctrl-c to stop)")
        for report in session.monitor.run(interval, iterations=args.iterations):
            print(report.summary())
            for closed in report.closed:
                print(
                    f"  #{closed.trade.trade_id} {closed.trade.symbol} "
                    f"{closed.reason.value} @ {closed.exit_price:.2f} "
                    f"pnl {closed.trade.realized_pnl or 0.0:+.2f} "
                    f"({len(closed.lessons)} lesson(s) written)"
                )
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        session.close()
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    session = _system(args).for_client(args.client)
    try:
        report = session.monitor.close_manually(args.symbol, args.note)
        print(report.summary())
        for closed in report.closed:
            print(f"  pnl {closed.trade.realized_pnl or 0.0:+.2f}, lesson written")
        for error in report.errors:
            print(f"  {error}")
        return 1 if report.errors else 0
    finally:
        session.close()


def cmd_feedback(args: argparse.Namespace) -> int:
    """The human-in-the-loop entry point."""
    session = _system(args).for_client(args.client)
    try:
        receipt = session.reflection.ingest_feedback(args.text, trade_id=args.trade)
        print(f"stored in {session.client_id} memory as {receipt.memory_id}")
        print(f"directives: {receipt.describe()}")
        print("takes effect on the next cycle")
    finally:
        session.close()
    return 0


def cmd_directives(args: argparse.Namespace) -> int:
    session = _system(args).for_client(args.client)
    try:
        if args.retire is not None:
            ok = session.reflection.retire_directive(args.retire)
            print(f"directive #{args.retire} {'retired' if ok else 'not found'}")
            return 0 if ok else 1
        for directive in session.reflection.active_directives():
            scope = (
                "/".join(directive.symbols)
                or "/".join(s.value for s in directive.setups)
                or "global"
            )
            print(
                f"#{directive.directive_id:<4} {directive.action.value:<14} "
                f"{scope:<28} {directive.source_text[:60]}"
            )
    finally:
        session.close()
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    session = _system(args).for_client(args.client)
    try:
        for text in session.reflection.recall(args.query, k=args.k):
            print(f"- {text}")
    finally:
        session.close()
    return 0


def cmd_journal(args: argparse.Namespace) -> int:
    session = _system(args).for_client(args.client)
    try:
        halted, reason = session.journal.halt_state()
        print(f"halted: {halted}{f' ({reason})' if reason else ''}")
        for trade in session.journal.open_trades():
            print(
                f"  open  #{trade.trade_id} {trade.symbol} {trade.setup.value} "
                f"x{trade.qty} @ {trade.entry_price:.2f} stop {trade.stop_price:.2f}"
            )
        for row in session.journal.performance_by_setup():
            print(
                f"  stats {row['setup']:<20} n={row['n']} wins={row['wins']} "
                f"pnl={row['pnl']}"
            )
    finally:
        session.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradesys", description=__doc__)
    parser.add_argument("--root", help="project root holding clients/ and var/")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("clients", help="list configured tenants").set_defaults(func=cmd_clients)

    run = sub.add_parser("run", help="run one trading cycle for a client")
    run.add_argument("--client", required=True)
    run.set_defaults(func=cmd_run)

    monitor = sub.add_parser("monitor", help="poll open positions and manage exits")
    monitor.add_argument("--client", required=True)
    monitor.add_argument("--interval", type=float, help="seconds between sweeps")
    monitor.add_argument("--iterations", type=int, help="stop after N sweeps")
    monitor.set_defaults(func=cmd_monitor)

    close = sub.add_parser("close", help="close a position now and record the lesson")
    close.add_argument("--client", required=True)
    close.add_argument("--note", default="", help="why you are closing it")
    close.add_argument("symbol")
    close.set_defaults(func=cmd_close)

    feedback = sub.add_parser("feedback", help="teach a client's agents something")
    feedback.add_argument("--client", required=True)
    feedback.add_argument("--trade", type=int, help="attach the feedback to a trade id")
    feedback.add_argument("text", help="free-text guidance, in quotes")
    feedback.set_defaults(func=cmd_feedback)

    directives = sub.add_parser("directives", help="list or retire active directives")
    directives.add_argument("--client", required=True)
    directives.add_argument("--retire", type=int, help="directive id to withdraw")
    directives.set_defaults(func=cmd_directives)

    recall = sub.add_parser("recall", help="query a client's memory")
    recall.add_argument("--client", required=True)
    recall.add_argument("--k", type=int, default=5)
    recall.add_argument("query")
    recall.set_defaults(func=cmd_recall)

    journal = sub.add_parser("journal", help="show open trades and per-setup stats")
    journal.add_argument("--client", required=True)
    journal.set_defaults(func=cmd_journal)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except TradesysError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

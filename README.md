# tradesys

A multi-tenant, agentic trading framework. One codebase runs N isolated clients,
each with its own watchlist, risk limits, broker keys and learned memory.

**This is engineering scaffolding, not investment advice.** The technical setups in
`agents/scout.py` are structural placeholders showing where signal logic plugs in —
they are not a validated edge. Research and backtest your own before risking capital.
Live execution is locked behind two independent switches.

---

## Architecture

```
scout ──▶ candidates ──▶ RISK MANAGER ──▶ RiskDecision ──▶ execution ──▶ broker
  ▲                      (deterministic)                                    │
  │                                                                         ▼
  │                                            POSITION MONITOR ◀──── open positions
  │                                          (stop / target / time /         │
  │                                           broker reconciliation)         │
  │                                                    │ closes              │
  └────────────── memory (per-tenant vectors) ◀── reflection ◀───────────────┘
                                    ▲
                                    └── human feedback (CLI)
```

The load-bearing decisions:

1. **The Risk Manager is not an agent that reasons.** It is a hardcoded rule engine
   with no model calls and no randomness. It is the only component that constructs an
   entry `OrderRequest`, and `ExecutionAgent.execute()` accepts nothing but an approved
   `RiskDecision`. There is no code path from a model's output to the broker.

2. **The Position Monitor is the other half of it.** The Risk Manager decides what risk
   may be *taken*; the monitor is what *removes* it. Exits are never gated by risk checks
   or by a halt — reducing exposure is always allowed. Closing a trade and reflecting on
   it are one unit of work in `_close()`, not two schedules that can drift apart, so a
   trade cannot close without producing a lesson.

3. **Human feedback is compiled, not just embedded.** Free text is stored verbatim for
   RAG *and* parsed into a structured `FeedbackDirective` with a closed vocabulary
   (this tenant's own symbols and setups). An `AVOID` directive is enforced in code, in
   `apply_directives()`. Storing the prose alone would make "avoid breakouts" depend on
   whether the model happened to attend to it in the next prompt.

4. **Isolation lives in one file.** `TenantContext` is the only thing that derives a
   data path, a SQLite filename or a vector collection name. Stores are built *from* a
   context and re-check ownership on every write, so a mis-wired object raises instead
   of leaking.

5. **Nothing is global.** Two tenants in one process share only the `Settings` object.
   Credentials are resolved per `client_id` by a `SecretResolver`; the client YAML holds
   the *name* of the env var, never a key.

## Layout

```
src/tradesys/
  core/          tenancy, models, enums, config, clock, logging, errors
  clients/       ClientProfile, RiskProfile, YAML loader, secret resolution
  data/          market data providers + indicators
  memory/        vector stores (local/Chroma), embeddings, directive extraction
  agents/        scout, risk_manager, execution, monitor, reflection
  brokers/       Broker protocol, paper simulator, Alpaca
  persistence/   per-tenant SQLite journal (trades, orders, directives, risk state)
  orchestration/ TradingSystem factory + per-tenant cycle
  cli/           operator interface
clients/         one YAML per tenant
tests/           risk limits, tenant isolation, the feedback loop, exit management
examples/        runnable end-to-end demo
```

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -e ".[dev]"
python examples/run_client.py
```

## Operating it

```bash
tradesys clients
tradesys run --client tal_dahan
tradesys monitor --client tal_dahan --interval 30
tradesys close --client tal_dahan RIVN --note "taking risk off before the print"
tradesys feedback --client tal_dahan --trade 45 "Trade #45 on RIVN was great, prioritize this setup"
tradesys feedback --client client_001 "Adjust strategy to avoid buying breakouts"
tradesys directives --client tal_dahan
tradesys directives --client tal_dahan --retire 3
tradesys recall --client tal_dahan "RIVN breakout"
tradesys journal --client tal_dahan
```

## Running the exit manager

`PositionMonitor.check_once()` is a pure sweep over current price and open positions,
so it fits three deployment shapes:

| Shape | How | When |
|---|---|---|
| Inline | already wired — `run_cycle()` calls it first, before any new entry | daily / EOD cadence |
| Dedicated loop | `tradesys monitor --client X --interval 30` | intraday, one process per tenant |
| Background thread | `thread, stop = session.monitor.start_background(30)` | one process, several tenants |

The background form is safe because `Journal` holds its SQLite connection under a lock
in WAL mode. Set the stop event and join the thread before `session.close()`.

Exit precedence is deliberate: **broker reconciliation → stop → target → time stop.**
Reconciliation first, so a position already gone (a bracket order that filled
server-side) is booked rather than "sold" again. Stop before target, so a bar that
gapped through both is booked as the loss — assuming the good fill would flatter every
P&L report built on this journal.

## Dashboard

```bash
pip install -e ".[dashboard]"
streamlit run app.py
```

Opens at http://127.0.0.1:8501. Four tabs per tenant — Overview, Charts, Agent
activity, Controls — all scoped to one `ClientSession`, so switching clients
switches journal, memory, broker and keys together.

Mutating controls are inert until **Arm controls** is switched on in the sidebar,
and it resets on reload so a stale tab cannot fire a trade. Destructive actions
(clearing a halt, enabling automation) additionally require typing the client id.

### Admin control center

A hidden route at `?admin=1`, with no link to it from the client console:

```
http://127.0.0.1:8501/?admin=1
```

Requires `TRADESYS_ADMIN_PASSCODE`. If that variable is unset the route is closed
entirely rather than open — fail closed. Tabs: fleet overview, billing/lifecycle
kill-switch, shadow mode, marketing export, hive mind.

Three constraints are built into it deliberately:

- **Freezing binds in the backend.** Suspending a tenant writes a halt into that
  tenant's own Journal, so the deterministic Risk Manager blocks entries from every
  surface — dashboard, CLI or cron — not just from the UI that set the flag. Exits
  stay available: a suspended client is never trapped in a position.
- **The hive mind pools statistics, never prose.** It aggregates setup, outcome,
  R-multiple and holding period across tenants. It never reads `MemoryRecord.text`,
  because postmortem rationales and operator feedback are client-identifying, and a
  "global lesson" assembled from them would be a privacy leak wearing a helpful hat.
- **Simulated performance carries its own context.** Shadow mode and the marketing
  export both stamp the hypothetical-performance disclosure and show the tenant's
  full record — total trades, win rate, net P&L — around any highlighted winners.
  Publishing selected green trades from a losing book is cherry-picking; the export
  states the net figure so you do not do it by accident. Nothing is posted anywhere
  from the UI: it produces a draft for you to review and publish yourself.

Neither passcode is authentication — one shared secret, no identities, no per-action
audit trail. With fleet-wide kill switches behind it, put a real identity proxy in
front before this leaves localhost.

## Portal (Hebrew, RTL)

```bash
pip install -e ".[dashboard]"
streamlit run app.py
```

Entry screen offers three paths plus a demo:

| Path | Access |
|---|---|
| Demo mode | No registration. Runs the real engine on synthetic data. |
| Client login | Per-client account, PBKDF2-hashed password. |
| Self-registration | Creates a *pending* lead, not a tradeable account. |
| Admin portal | `TRADESYS_ADMIN_PASSCODE`; fails closed if unset. |

### Demo mode

`clients/demo_showcase.yaml` is a real tenant running the real agent stack against
`SyntheticMarketData` and the paper broker. Its history was produced by running the
engine over 40 simulated trading days and keeping whatever came out — 12 trades,
6 wins, 6 losses, and one genuine drawdown halt. Nothing was re-rolled to look good,
because a demo built from selected outcomes is a fabricated track record, not a demo.

A persistent disclosure banner states the results are simulated. Broker connection,
automation and emergency controls are replaced by the upgrade CTA; running a cycle
stays enabled, since that is the part worth showing.

### Self-registration

Registration creates a `Registration` record and an **inactive** account. It does
not create a `ClientProfile`, so a prospect cannot provision themselves something
that can place orders. An admin approves from the "פניות הרשמה" tab, which writes a
conservative starter profile (paper broker, 2% max drawdown, 0.5% per trade, no
shorting, no leverage) and activates the login.

No email verification, rate limiting or CAPTCHA — this is lead capture on a trusted
network, not public internet onboarding.

### Hebrew feedback

`memory/directives.py` carries a Hebrew vocabulary alongside the English one. Without
it, `"הימנע מפריצות"` degraded to an advisory NOTE and the hard veto silently never
fired — the whole feedback loop would have been decorative in a Hebrew UI. Covered by
`test_hebrew_avoid_feedback_becomes_a_binding_veto`.

## Onboarding a new client

Add `clients/<client_id>.yaml`. That is the whole procedure — no code changes. The
`client_id` must match `^[a-z][a-z0-9_]{2,39}$`; it is validated before it is ever
interpolated into a path or collection name.

## Going live

Two independent locks, both required:

1. `broker.paper: false` in the tenant's YAML, and
2. `TRADESYS_ALLOW_LIVE_TRADING=1` in the process environment.

`ExecutionAgent` refuses to construct otherwise. Before flipping either, replace the
placeholder setups, backtest, and run the paper loop long enough to see the risk limits
actually fire.

## Testing

```bash
pytest
```

Covers per-tenant position sizing, every rejection path in the Risk Manager, latching
drawdown halts, cross-tenant memory isolation, feedback becoming a binding veto, and
exit management: stop/target/time triggers, broker reconciliation, exits during a halt,
and the guarantee that a data outage never closes a position.

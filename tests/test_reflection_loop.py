from __future__ import annotations

from conftest import make_candidate, make_profile

from tradesys.agents.reflection import ReflectionAgent
from tradesys.agents.scout import ScoutAgent
from tradesys.core.enums import DirectiveAction, SetupType
from tradesys.core.tenancy import TenantContext
from tradesys.data.market_data import SyntheticMarketData
from tradesys.memory.directives import apply_directives
from tradesys.memory.store import LocalVectorMemory
from tradesys.persistence.journal import Journal


def build(tmp_path, clock, client_id="tal_dahan"):
    profile = make_profile(client_id)
    tenant = TenantContext.create(client_id, tmp_path)
    journal = Journal(tenant)
    memory = LocalVectorMemory(tenant)
    reflection = ReflectionAgent(tenant, profile, memory, journal, clock=clock)
    return tenant, profile, journal, memory, reflection


def test_avoid_feedback_becomes_a_binding_veto(tmp_path, clock):
    tenant, profile, journal, memory, reflection = build(tmp_path, clock)

    receipt = reflection.ingest_feedback("Adjust strategy to avoid buying breakouts")
    assert receipt.directives[0].action is DirectiveAction.AVOID
    assert SetupType.BREAKOUT in receipt.directives[0].setups

    scout = ScoutAgent(
        tenant,
        profile,
        SyntheticMarketData(),
        memory,
        reflection.active_directives(),
        clock,
    )
    vetoed, _, notes = apply_directives(
        reflection.active_directives(), make_candidate(setup=SetupType.BREAKOUT)
    )
    assert vetoed
    assert notes
    assert all(c.setup is not SetupType.BREAKOUT for c in scout.scan())


def test_prioritize_feedback_raises_confidence(tmp_path, clock):
    _, _, _, _, reflection = build(tmp_path, clock)
    reflection.ingest_feedback("RIVN breakouts have been great, prioritize this setup")

    vetoed, multiplier, _ = apply_directives(
        reflection.active_directives(), make_candidate(symbol="RIVN")
    )
    assert not vetoed
    assert multiplier > 1.0


def test_feedback_is_retrievable_from_that_tenants_memory(tmp_path, clock):
    _, _, _, _, reflection = build(tmp_path, clock)
    reflection.ingest_feedback("KULR pullbacks keep failing into earnings")
    assert any("KULR" in text for text in reflection.recall("KULR pullback", k=3))


def test_unqualified_avoid_does_not_halt_the_whole_book(tmp_path, clock):
    _, _, _, _, reflection = build(tmp_path, clock)
    receipt = reflection.ingest_feedback("avoid everything, I am nervous")
    # A global hard veto would silently disable the tenant, so it degrades to advice.
    assert receipt.directives[0].action is not DirectiveAction.AVOID


def test_a_directive_can_be_withdrawn(tmp_path, clock):
    _, _, _, _, reflection = build(tmp_path, clock)
    receipt = reflection.ingest_feedback("avoid buying breakouts")
    directive_id = receipt.directives[0].directive_id

    assert reflection.retire_directive(directive_id)
    assert not any(
        d.directive_id == directive_id for d in reflection.active_directives()
    )


def test_operator_feedback_cannot_target_an_off_watchlist_symbol(tmp_path, clock):
    _, _, _, _, reflection = build(tmp_path, clock)
    receipt = reflection.ingest_feedback("prioritize TSLA breakouts")
    for directive in receipt.directives:
        assert "TSLA" not in directive.symbols


def test_hebrew_avoid_feedback_becomes_a_binding_veto(tmp_path, clock):
    """The operator UI is Hebrew, so Hebrew must reach the same hard veto.

    Regression guard: with an English-only vocabulary this degraded to an advisory
    NOTE and the veto silently never fired.
    """
    _, _, _, _, reflection = build(tmp_path, clock)
    receipt = reflection.ingest_feedback("הימנע מפריצות, הן נכשלות")

    assert receipt.directives[0].action is DirectiveAction.AVOID
    assert SetupType.BREAKOUT in receipt.directives[0].setups

    vetoed, _, _ = apply_directives(
        reflection.active_directives(), make_candidate(setup=SetupType.BREAKOUT)
    )
    assert vetoed


def test_hebrew_prioritize_feedback_raises_confidence(tmp_path, clock):
    _, _, _, _, reflection = build(tmp_path, clock)
    reflection.ingest_feedback("תעדף פולבקים על RIVN")

    vetoed, multiplier, _ = apply_directives(
        reflection.active_directives(),
        make_candidate(symbol="RIVN", setup=SetupType.PULLBACK),
    )
    assert not vetoed
    assert multiplier > 1.0

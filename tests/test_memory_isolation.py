from __future__ import annotations

import pytest

from tradesys.core.enums import MemoryKind
from tradesys.core.errors import ConfigError, TenantIsolationError
from tradesys.core.identity import validate_client_id
from tradesys.core.models import MemoryRecord
from tradesys.core.tenancy import TenantContext
from tradesys.memory.store import LocalVectorMemory


def record(client_id: str, text: str) -> MemoryRecord:
    return MemoryRecord(client_id=client_id, kind=MemoryKind.HUMAN_FEEDBACK, text=text)


def test_lessons_do_not_bleed_between_tenants(tmp_path):
    a = LocalVectorMemory(TenantContext.create("tal_dahan", tmp_path))
    b = LocalVectorMemory(TenantContext.create("client_001", tmp_path))

    a.add([record("tal_dahan", "RIVN breakouts have been excellent, prioritize them")])
    b.add([record("client_001", "avoid RIVN entirely, the client dislikes EV exposure")])

    a_hits = [hit.record.text for hit in a.query("RIVN breakout", k=5)]
    b_hits = [hit.record.text for hit in b.query("RIVN breakout", k=5)]

    assert len(a_hits) == 1 and "prioritize" in a_hits[0]
    assert len(b_hits) == 1 and "dislikes" in b_hits[0]
    assert a.count() == 1 and b.count() == 1


def test_store_rejects_a_foreign_record(tmp_path):
    memory = LocalVectorMemory(TenantContext.create("tal_dahan", tmp_path))
    with pytest.raises(TenantIsolationError):
        memory.add([record("client_001", "not mine")])


def test_tenants_get_distinct_paths_and_collections(tmp_path):
    a = TenantContext.create("tal_dahan", tmp_path)
    b = TenantContext.create("client_001", tmp_path)
    assert a.memory_path != b.memory_path
    assert a.journal_path != b.journal_path
    assert a.memory_collection != b.memory_collection


@pytest.mark.parametrize(
    "bad_id", ["../escape", "Tal", "a", "with space", "x" * 41, "9lives", ""]
)
def test_client_id_is_validated_before_touching_the_filesystem(bad_id):
    with pytest.raises(ConfigError):
        validate_client_id(bad_id)


def test_context_rejects_objects_from_another_tenant(tmp_path):
    tenant = TenantContext.create("tal_dahan", tmp_path)
    with pytest.raises(TenantIsolationError):
        tenant.assert_owns(record("client_001", "foreign"))

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from tradesys.core.enums import MemoryKind
from tradesys.core.errors import TenantIsolationError
from tradesys.core.models import MemoryHit, MemoryRecord
from tradesys.core.tenancy import TenantContext
from tradesys.memory.embeddings import Embedder, HashingEmbedder, cosine


class VectorMemory(Protocol):
    """A tenant-scoped vector store.

    Implementations are constructed *from a TenantContext* and must never be able to
    read or write another tenant's records, regardless of the arguments they are
    passed at call time.
    """

    client_id: str

    def add(self, records: Sequence[MemoryRecord]) -> list[str]: ...

    def query(
        self,
        text: str,
        *,
        k: int = 5,
        kinds: Iterable[MemoryKind] | None = None,
        where: Mapping[str, Any] | None = None,
    ) -> list[MemoryHit]: ...

    def recent(
        self, limit: int = 20, kinds: Iterable[MemoryKind] | None = None
    ) -> list[MemoryRecord]: ...

    def count(self) -> int: ...


def _serialise(record: MemoryRecord, record_id: str, vector: list[float]) -> dict:
    return {
        "record_id": record_id,
        "client_id": record.client_id,
        "kind": record.kind.value,
        "text": record.text,
        "metadata": dict(record.metadata),
        "created_at": (record.created_at or datetime.now(timezone.utc)).isoformat(),
        "vector": vector,
    }


class LocalVectorMemory:
    """File-backed vector memory, one JSON document per tenant.

    Zero external dependencies, so the whole reflection loop runs offline and in CI.
    The on-disk layout is a tenant-private file under the tenant's data directory.
    """

    def __init__(self, tenant: TenantContext, embedder: Embedder | None = None) -> None:
        self._tenant = tenant
        self.client_id = tenant.client_id
        self._embedder = embedder or HashingEmbedder()
        self._path: Path = tenant.memory_path
        self._rows: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        rows = json.loads(self._path.read_text(encoding="utf-8"))
        foreign = {r["client_id"] for r in rows} - {self.client_id}
        if foreign:
            raise TenantIsolationError(
                f"{self._path} contains records for {sorted(foreign)}"
            )
        return rows

    def _flush(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._rows, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)

    def add(self, records: Sequence[MemoryRecord]) -> list[str]:
        for record in records:
            self._tenant.assert_owns(record)
        if not records:
            return []
        vectors = self._embedder.embed([r.text for r in records])
        ids: list[str] = []
        for record, vector in zip(records, vectors):
            record_id = record.record_id or uuid.uuid4().hex
            self._rows.append(_serialise(record, record_id, vector))
            ids.append(record_id)
        self._flush()
        return ids

    def query(
        self,
        text: str,
        *,
        k: int = 5,
        kinds: Iterable[MemoryKind] | None = None,
        where: Mapping[str, Any] | None = None,
    ) -> list[MemoryHit]:
        if not self._rows or k <= 0:
            return []
        wanted_kinds = {kind.value for kind in kinds} if kinds else None
        probe = self._embedder.embed([text])[0]

        scored: list[MemoryHit] = []
        for row in self._rows:
            if row["client_id"] != self.client_id:
                continue  # defence in depth; _load already rejects foreign rows
            if wanted_kinds and row["kind"] not in wanted_kinds:
                continue
            if where and any(row["metadata"].get(key) != val for key, val in where.items()):
                continue
            scored.append(
                MemoryHit(
                    record=MemoryRecord(
                        client_id=row["client_id"],
                        kind=MemoryKind(row["kind"]),
                        text=row["text"],
                        metadata=row["metadata"],
                        record_id=row["record_id"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                    ),
                    score=cosine(probe, row["vector"]),
                )
            )
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:k]

    def recent(
        self, limit: int = 20, kinds: Iterable[MemoryKind] | None = None
    ) -> list[MemoryRecord]:
        """Newest records first. Chronological, not similarity-ranked."""
        wanted_kinds = {kind.value for kind in kinds} if kinds else None
        records = [
            MemoryRecord(
                client_id=row["client_id"],
                kind=MemoryKind(row["kind"]),
                text=row["text"],
                metadata=row["metadata"],
                record_id=row["record_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in self._rows
            if row["client_id"] == self.client_id
            and (not wanted_kinds or row["kind"] in wanted_kinds)
        ]
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        records.sort(key=lambda r: r.created_at or oldest, reverse=True)
        return records[:limit]

    def count(self) -> int:
        return len(self._rows)

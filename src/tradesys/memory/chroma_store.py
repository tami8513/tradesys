from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from tradesys.core.enums import MemoryKind
from tradesys.core.models import MemoryHit, MemoryRecord
from tradesys.core.tenancy import TenantContext
from tradesys.memory.embeddings import Embedder, HashingEmbedder


class ChromaVectorMemory:
    """ChromaDB-backed memory with one physical collection per tenant.

    Isolation is enforced three times over, on purpose:
      1. the collection name is derived from the tenant context and nothing else;
      2. every record carries client_id in its metadata;
      3. every query is filtered on that client_id.

    Layers 2 and 3 are redundant while layer 1 holds. They exist so that a future
    change to the collection strategy (e.g. one shared collection for cost reasons)
    cannot silently leak one client's lessons into another client's strategy.
    """

    def __init__(
        self,
        tenant: TenantContext,
        *,
        embedder: Embedder | None = None,
        persist_dir: str | None = None,
        client: Any = None,
    ) -> None:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "chromadb is not installed; use LocalVectorMemory or "
                "pip install chromadb"
            ) from exc

        self._tenant = tenant
        self.client_id = tenant.client_id
        self._embedder = embedder or HashingEmbedder()
        self._client = client or chromadb.PersistentClient(
            path=persist_dir or str(tenant.data_dir / "chroma")
        )
        self._collection = self._client.get_or_create_collection(
            name=tenant.memory_collection,
            metadata={"client_id": tenant.client_id, "hnsw:space": "cosine"},
        )

    def add(self, records: Sequence[MemoryRecord]) -> list[str]:
        for record in records:
            self._tenant.assert_owns(record)
        if not records:
            return []

        ids = [r.record_id or uuid.uuid4().hex for r in records]
        documents = [r.text for r in records]
        metadatas = [
            {
                **{k: v for k, v in r.metadata.items() if v is not None},
                "client_id": self.client_id,
                "kind": r.kind.value,
                "created_at": (r.created_at or datetime.now(timezone.utc)).isoformat(),
            }
            for r in records
        ]
        self._collection.add(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=self._embedder.embed(documents),
        )
        return ids

    def _where(
        self,
        kinds: Iterable[MemoryKind] | None,
        where: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        clauses: list[dict[str, Any]] = [{"client_id": {"$eq": self.client_id}}]
        if kinds:
            clauses.append({"kind": {"$in": [k.value for k in kinds]}})
        for key, value in (where or {}).items():
            clauses.append({key: {"$eq": value}})
        return clauses[0] if len(clauses) == 1 else {"$and": clauses}

    def query(
        self,
        text: str,
        *,
        k: int = 5,
        kinds: Iterable[MemoryKind] | None = None,
        where: Mapping[str, Any] | None = None,
    ) -> list[MemoryHit]:
        if k <= 0:
            return []
        result = self._collection.query(
            query_embeddings=self._embedder.embed([text]),
            n_results=k,
            where=self._where(kinds, where),
            include=["documents", "metadatas", "distances"],
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        ids = (result.get("ids") or [[]])[0]

        hits: list[MemoryHit] = []
        for doc, meta, distance, record_id in zip(documents, metadatas, distances, ids):
            meta = dict(meta or {})
            if meta.get("client_id") != self.client_id:
                continue
            created = meta.get("created_at")
            hits.append(
                MemoryHit(
                    record=MemoryRecord(
                        client_id=self.client_id,
                        kind=MemoryKind(meta.get("kind", MemoryKind.SESSION_LESSON.value)),
                        text=doc,
                        metadata=meta,
                        record_id=record_id,
                        created_at=datetime.fromisoformat(created) if created else None,
                    ),
                    score=1.0 - float(distance),
                )
            )
        return hits

    def recent(
        self, limit: int = 20, kinds: Iterable[MemoryKind] | None = None
    ) -> list[MemoryRecord]:
        """Newest records first.

        Chroma has no server-side ordering, so this pulls the tenant's matching
        records and sorts on created_at here. Fine at journal scale; if a tenant's
        memory grows large, move this to a metadata index.
        """
        result = self._collection.get(
            where=self._where(kinds, None),
            include=["documents", "metadatas"],
        )
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        ids = result.get("ids") or []

        records: list[MemoryRecord] = []
        for doc, meta, record_id in zip(documents, metadatas, ids):
            meta = dict(meta or {})
            if meta.get("client_id") != self.client_id:
                continue
            created = meta.get("created_at")
            records.append(
                MemoryRecord(
                    client_id=self.client_id,
                    kind=MemoryKind(meta.get("kind", MemoryKind.SESSION_LESSON.value)),
                    text=doc,
                    metadata=meta,
                    record_id=record_id,
                    created_at=datetime.fromisoformat(created) if created else None,
                )
            )
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        records.sort(key=lambda r: r.created_at or oldest, reverse=True)
        return records[:limit]

    def count(self) -> int:
        return int(self._collection.count())

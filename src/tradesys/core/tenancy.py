from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tradesys.core.errors import TenantIsolationError
from tradesys.core.identity import validate_client_id


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The single source of truth for *where a tenant's data lives*.

    Nothing in the system derives a path, a table filter or a vector collection name
    on its own; it asks the context. That keeps isolation auditable in one file
    instead of scattered across every store.
    """

    client_id: str
    data_dir: Path

    @classmethod
    def create(cls, client_id: str, data_root: Path) -> "TenantContext":
        cid = validate_client_id(client_id)
        tenant_dir = (data_root / cid).resolve()
        root = data_root.resolve()
        if root not in tenant_dir.parents and tenant_dir != root:
            raise TenantIsolationError(f"tenant dir escapes data root: {tenant_dir}")
        tenant_dir.mkdir(parents=True, exist_ok=True)
        return cls(client_id=cid, data_dir=tenant_dir)

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.db"

    @property
    def memory_path(self) -> Path:
        return self.data_dir / "memory.json"

    @property
    def memory_collection(self) -> str:
        """Vector-store namespace. Physically separate per tenant, never shared."""
        return f"tenant_{self.client_id}_memory"

    def assert_owns(self, obj: Any) -> None:
        """Guard against a mis-wired object carrying another tenant's data."""
        other = getattr(obj, "client_id", None)
        if other is not None and other != self.client_id:
            raise TenantIsolationError(
                f"object belongs to client_id={other!r}, "
                f"but context is {self.client_id!r}"
            )

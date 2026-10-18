"""Per-tenant automation switch, shared by the dashboard and the scheduler.

The dashboard's toggle writes this file; the scheduler reads it before every
cycle. A tenant with no file falls back to TRADESYS_AUTORUN (a comma-separated
list of client ids), so a fresh server can start trading the demo without anyone
clicking first, while an explicit choice in the UI always wins.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

AUTOMATION_FILE = "automation.json"
LIFECYCLE_FILE = "lifecycle.json"


def default_autorun() -> frozenset[str]:
    raw = os.getenv("TRADESYS_AUTORUN", "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def automation_enabled(tenant_dir: Path, client_id: str) -> bool:
    path = Path(tenant_dir) / AUTOMATION_FILE
    if path.exists():
        try:
            return bool(json.loads(path.read_text(encoding="utf-8")).get("enabled"))
        except (ValueError, OSError):
            return False
    return client_id in default_autorun()


def set_automation(tenant_dir: Path, enabled: bool) -> None:
    (Path(tenant_dir) / AUTOMATION_FILE).write_text(
        json.dumps(
            {"enabled": bool(enabled), "updated_at": datetime.now(timezone.utc).isoformat()}
        ),
        encoding="utf-8",
    )


def lifecycle_active(tenant_dir: Path) -> bool:
    """False when the operator has suspended the tenant from the admin portal."""
    path = Path(tenant_dir) / LIFECYCLE_FILE
    if not path.exists():
        return True
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("active", True))
    except (ValueError, OSError):
        return True

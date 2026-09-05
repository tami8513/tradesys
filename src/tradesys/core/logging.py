from __future__ import annotations

import logging
from typing import Any

_CONFIGURED = False


def configure(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    )
    _CONFIGURED = True


class TenantLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return f"[client={self.extra['client_id']}] {msg}", kwargs


def get_logger(name: str, client_id: str) -> logging.LoggerAdapter:
    """Every log line in the system is stamped with its tenant."""
    return TenantLoggerAdapter(logging.getLogger(name), {"client_id": client_id})

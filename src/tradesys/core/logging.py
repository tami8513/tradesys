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
    # httpx logs every request URL at INFO. Finnhub takes its API key as a query
    # parameter, so that would write the key into the server logs in plain text.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


class TenantLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return f"[client={self.extra['client_id']}] {msg}", kwargs


def get_logger(name: str, client_id: str) -> logging.LoggerAdapter:
    """Every log line in the system is stamped with its tenant."""
    return TenantLoggerAdapter(logging.getLogger(name), {"client_id": client_id})

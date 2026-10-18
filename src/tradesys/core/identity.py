from __future__ import annotations

import re

from tradesys.core.errors import ConfigError

_CLIENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")


def is_valid_client_id(client_id: str) -> bool:
    return isinstance(client_id, str) and bool(_CLIENT_ID_RE.match(client_id))


def validate_client_id(client_id: str) -> str:
    """Return ``client_id`` if it is safe to use as a filesystem and collection name.

    ``client_id`` is interpolated into directory paths, SQLite filenames and vector
    collection names, so it is treated as untrusted input at every entry point.
    """
    if not is_valid_client_id(client_id):
        raise ConfigError(
            f"invalid client_id {client_id!r}: expected ^[a-z][a-z0-9_]{{2,39}}$"
        )
    return client_id

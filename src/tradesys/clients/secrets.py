from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Protocol

from tradesys.clients.profile import ClientProfile
from tradesys.core.errors import CredentialError


@dataclass(frozen=True, slots=True)
class BrokerCredentials:
    key_id: str
    secret_key: str
    base_url: str
    paper: bool

    def __repr__(self) -> str:
        # Keeps secrets out of tracebacks, logs and debugger output.
        return f"BrokerCredentials(key_id={self.key_id[:4]}..., paper={self.paper})"


class SecretResolver(Protocol):
    def broker_credentials(self, profile: ClientProfile) -> BrokerCredentials: ...


class EnvSecretResolver:
    """Resolves per-tenant broker keys from the environment.

    Naming: TRADESYS__<CLIENT_ID>__<BROKER>__KEY_ID and __SECRET_KEY, or the
    explicit credential_ref prefix declared in the profile. Secrets never live in
    client YAML, only the *name* of the variable that holds them.

    Swap this for a Vault / AWS Secrets Manager / GCP Secret Manager implementation
    without touching a single agent.
    """

    _PAPER_URL = "https://paper-api.alpaca.markets"
    _LIVE_URL = "https://api.alpaca.markets"

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = env if env is not None else os.environ

    def _prefix(self, profile: ClientProfile) -> str:
        if profile.broker.credential_ref:
            return profile.broker.credential_ref
        return f"TRADESYS__{profile.client_id.upper()}__{profile.broker.name.upper()}"

    def broker_credentials(self, profile: ClientProfile) -> BrokerCredentials:
        prefix = self._prefix(profile)
        key_id = self._env.get(f"{prefix}__KEY_ID", "")
        secret_key = self._env.get(f"{prefix}__SECRET_KEY", "")
        if not key_id or not secret_key:
            raise CredentialError(
                f"missing broker credentials for client {profile.client_id!r}: "
                f"expected {prefix}__KEY_ID and {prefix}__SECRET_KEY in the environment"
            )
        base_url = profile.broker.base_url or (
            self._PAPER_URL if profile.broker.paper else self._LIVE_URL
        )
        return BrokerCredentials(
            key_id=key_id,
            secret_key=secret_key,
            base_url=base_url,
            paper=profile.broker.paper,
        )

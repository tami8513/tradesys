from __future__ import annotations

from abc import ABC

from tradesys.clients.profile import ClientProfile
from tradesys.core.clock import Clock, SystemClock
from tradesys.core.logging import get_logger
from tradesys.core.tenancy import TenantContext


class Agent(ABC):
    """Base class for every agent.

    An agent is always constructed for one tenant and holds that tenant's context,
    profile and logger. There is no way to point an existing agent at another tenant.
    """

    name: str = "agent"

    def __init__(
        self,
        tenant: TenantContext,
        profile: ClientProfile,
        clock: Clock | None = None,
    ) -> None:
        if profile.client_id != tenant.client_id:
            raise ValueError(
                f"profile {profile.client_id!r} does not match tenant {tenant.client_id!r}"
            )
        self.tenant = tenant
        self.profile = profile
        self.clock = clock or SystemClock()
        self.log = get_logger(f"tradesys.{self.name}", tenant.client_id)

    @property
    def client_id(self) -> str:
        return self.tenant.client_id

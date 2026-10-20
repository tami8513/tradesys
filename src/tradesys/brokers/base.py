from __future__ import annotations

from typing import Protocol

from tradesys.core.models import AccountSnapshot, OrderRequest, OrderResult, Position


class Broker(Protocol):
    """A tenant-bound broker connection.

    Implementations are constructed with one tenant's credentials and expose no way
    to act on behalf of another tenant.
    """

    client_id: str
    paper: bool

    def account(self) -> AccountSnapshot: ...

    def positions(self) -> list[Position]: ...

    def submit(self, order: OrderRequest) -> OrderResult: ...

    def close_position(self, symbol: str) -> OrderResult | None: ...

    def protect(self, symbol: str, qty: int, stop_price: float) -> None:
        """Make sure a resting server-side stop for `qty` sits at `stop_price`.

        Moves an existing stop, or places one when none is working (an expired
        leg, a manual cancel). A broker without resting orders has nothing to do.
        """

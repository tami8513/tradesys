"""tradesys — a multi-tenant, agentic trading framework.

Every runtime object in this package is bound to exactly one tenant (``client_id``).
There is no global, tenant-less state: see :mod:`tradesys.core.tenancy`.
"""

__version__ = "0.1.0"

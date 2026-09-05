from __future__ import annotations


class TradesysError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(TradesysError):
    """Malformed or missing tenant configuration."""


class TenantIsolationError(TradesysError):
    """An object was handed data belonging to a different tenant."""


class CredentialError(TradesysError):
    """Broker credentials are missing, incomplete, or not permitted."""


class BrokerError(TradesysError):
    """The broker rejected a request or returned an unusable response."""


class RiskViolation(TradesysError):
    """An order reached execution without a valid risk approval."""

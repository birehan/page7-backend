from __future__ import annotations


class ProviderError(Exception):
    """Base for every adapter-boundary error (architecture/06 §3). Every
    adapter catches its own SDK's exceptions and re-raises one of these before
    they leave the adapter module — no caller anywhere else in the codebase
    ever catches a vendor SDK exception type directly.
    """


class ProviderTimeoutError(ProviderError):
    pass


class ProviderRateLimitedError(ProviderError):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("provider rate limited")
        self.retry_after = retry_after


class ProviderAuthError(ProviderError):
    """Credential rejected (HTTP 401 / authentication_error)."""


class ProviderPaymentRequiredError(ProviderError):
    """HTTP 402 from a provider — billing suspended, plan gate, or free-tier capacity.

    ``reason`` carries the vendor code when known (e.g. Zernio
    ``free_tier_exceeded``). Capacity reasons must not hard-disable a
    credential; true billing/auth failures still do.
    """

    def __init__(
        self,
        message: str = "provider payment required",
        *,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason

    @property
    def is_capacity(self) -> bool:
        return self.reason == "free_tier_exceeded"


class ProviderContentFilteredError(ProviderError):
    pass


class ProviderUnavailableError(ProviderError):
    def __init__(
        self, message: str = "provider unavailable", *, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderNotFoundError(ProviderError):
    """HTTP 404 — resource already gone (e.g. account already deleted)."""


class AnalyticsCursorExpired(ProviderError):
    """GET /analytics/delta rejected a stale/malformed cursor (HTTP 400)."""

    def __init__(self, message: str = "analytics cursor expired") -> None:
        super().__init__(message)


class CredentialPoolExhausted(ProviderError):
    """No active Zernio credential has spare profile/account capacity."""

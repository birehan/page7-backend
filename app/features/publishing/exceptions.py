"""Domain exceptions for the publishing ledger (Phase 10)."""

from __future__ import annotations


class PublishingError(Exception):
    """Base for claim-time and result-application failures."""

    code: str = "PROVIDER_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ClaimCheckFailed(PublishingError):
    """Terminal claim-time check failure (no Zernio HTTP call)."""


class DoublePostDetected(PublishingError):
    """failed→published correction refused by ux_pub_published."""

    code = "DOUBLE_POST_DETECTED"

    def __init__(
        self,
        message: str = "Another publication for this post is already published",
    ) -> None:
        super().__init__(message, code=self.code)


class NonDefinitiveOutcome(PublishingError):
    """Publish result is non-definitive — caller must raise RetryableError."""

    code = "OUTCOME_UNKNOWN"

    def __init__(
        self,
        message: str = "Publish outcome is not yet definitive",
        *,
        retry_after_seconds: float = 15.0,
    ) -> None:
        super().__init__(message, code=self.code)
        self.retry_after_seconds = retry_after_seconds

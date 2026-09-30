"""Map vendor HTTP status failures into the shared ProviderError taxonomy.

Billing / auth failures are fallback-eligible at the task router (try the
configured secondary provider) but are not same-provider-retried — retrying
the identical Anthropic key after "credit balance too low" cannot help.
"""

from __future__ import annotations

from app.integrations.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderPaymentRequiredError,
    ProviderUnavailableError,
)

_BILLING_NEEDLES = (
    "credit balance",
    "insufficient credit",
    "insufficient credits",
    "insufficient_quota",
    "credit_balance_exhausted",
    "no credits remaining",
    "purchase credits",
    "plans & billing",
    "billing",
    "payment required",
    "exceeded your current quota",
    "quota exceeded",
)


def map_llm_http_error(status_code: int, message: str) -> ProviderError | None:
    """Return a taxonomy error for known HTTP failures, else ``None`` to re-raise."""
    if status_code >= 500:
        return ProviderUnavailableError(message)
    if status_code == 401:
        return ProviderAuthError()
    if status_code == 402:
        return ProviderPaymentRequiredError(message)
    # OpenAI often returns 429 for exhausted credit balance as well as true
    # rate limits; Anthropic billing usually arrives as 400. Treat either as
    # payment-required when the body mentions credits/quota.
    if status_code in {400, 429} and _looks_like_billing(message):
        return ProviderPaymentRequiredError(message)
    return None


def _looks_like_billing(message: str) -> bool:
    lower = message.lower()
    return any(needle in lower for needle in _BILLING_NEEDLES)

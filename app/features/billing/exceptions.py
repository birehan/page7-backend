"""Billing domain exceptions — internal, not the HTTP envelope."""

from __future__ import annotations


class BillingError(Exception):
    """Base for billing domain errors."""


class NoLiveSubscription(BillingError):
    """Organization has no live (trialing/active/past_due) subscription."""


class PlanNotFound(BillingError):
    """Requested plan code is unknown or inactive."""

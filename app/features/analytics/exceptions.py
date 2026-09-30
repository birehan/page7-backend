"""Analytics feature exceptions."""

from __future__ import annotations


class AnalyticsError(Exception):
    """Base for analytics domain errors."""


class AnalyticsGated(AnalyticsError):
    """Credential lacks the analytics add-on (402 / no access)."""

    def __init__(self, message: str = "analytics_addon_required") -> None:
        self.message = message
        super().__init__(message)

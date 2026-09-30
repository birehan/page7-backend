"""CrawlResult + lab exceptions (subset of pgblank shared AI ports)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CrawlResult:
    url: str
    title: str
    markdown: str
    metadata: dict[str, object] = field(default_factory=dict)
    branding: dict[str, object] = field(default_factory=dict)


class AiUnavailableError(Exception):
    """Firecrawl unreachable or returned a retryable/server error."""


class ConfigurationError(Exception):
    """Firecrawl rejected the request (bad URL / request shape)."""

"""Research-specific exception types and error codes (architecture/08 §7)."""

from __future__ import annotations


class ResearchError(Exception):
    code: str = "RESEARCH_NO_CONTENT"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ResearchFetchFailed(ResearchError):
    code = "RESEARCH_FETCH_FAILED"


class ResearchFetchTimeout(ResearchError):
    code = "RESEARCH_FETCH_TIMEOUT"


class ResearchRobotsDisallowed(ResearchError):
    code = "RESEARCH_ROBOTS_DISALLOWED"


class ResearchNoContent(ResearchError):
    code = "RESEARCH_NO_CONTENT"


def most_specific_code(codes: list[str]) -> str:
    """Prefer a specific fetch/robots failure over generic no-content."""
    order = (
        "RESEARCH_ROBOTS_DISALLOWED",
        "RESEARCH_FETCH_TIMEOUT",
        "RESEARCH_FETCH_FAILED",
        "RESEARCH_NO_CONTENT",
    )
    for preferred in order:
        if preferred in codes:
            return preferred
    return codes[0] if codes else "RESEARCH_NO_CONTENT"

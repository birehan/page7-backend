from __future__ import annotations

import hashlib
import logging
import sys
from typing import Any

import structlog

_REDACTED = "***REDACTED***"

# architecture/05 §10, verbatim — deliberately the same field set
# `users.deactivated_at`'s PDPL-erasure anonymization touches (02 §1); the two
# lists are kept in sync as a matter of process. `apikey` (no underscore) is
# Phase 1's own extra beyond the doc's list, kept as harmless additional
# coverage.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "secret",
        "token",
        "token_hash",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "totp_secret",
        "totp_secret_enc",
        "code_hash",
        "access_token",
        "refresh_token",
        "private_key",
        "email",
        "phone",
        "name",
        "name_ar",
        "ip",
        "vat_number",
        "participant_name",
        "participant_handle",
        "body",
    }
)


def redact_value(key: str, value: Any) -> Any:
    """Redact a single key/value the same way structlog and Sentry do."""
    if key.lower() not in SENSITIVE_KEYS:
        return value
    if key.lower() == "email" and isinstance(value, str):
        digest = hashlib.sha256(value.encode()).hexdigest()[:8]
        return f"sha256:{digest}"
    return _REDACTED


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy with sensitive keys redacted (architecture/05 §10)."""
    return {key: redact_value(key, value) for key, value in data.items()}


def _redact_processor(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Wired in from day one, not bolted on later: a key whose name looks like a
    secret is redacted before it ever reaches a sink, structured or not. An
    email is hashed rather than blanked — correlatable across log lines for
    debugging without being reversible.
    """
    for key in list(event_dict.keys()):
        event_dict[key] = redact_value(key, event_dict[key])
    return event_dict


def configure_logging(*, json_output: bool) -> None:
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_processor,
    ]
    renderer = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )

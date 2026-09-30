from __future__ import annotations

from typing import Any, cast

import sentry_sdk
from sentry_sdk.types import Event, Hint

from app.core.config import Settings
from app.core.logging import SENSITIVE_KEYS, redact_mapping, redact_value


def _redact_nested(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: redact_value(key, _redact_nested(item))
            if key.lower() in SENSITIVE_KEYS
            else _redact_nested(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_nested(item) for item in value]
    return value


def _before_send(event: Event, hint: Hint) -> Event | None:  # noqa: ARG001
    """architecture/13 §6 — identical field set as the structlog redactor."""
    mutable = cast(dict[str, Any], event)
    user = mutable.get("user")
    if isinstance(user, dict):
        mutable["user"] = redact_mapping(dict(user))
    extra = mutable.get("extra")
    if isinstance(extra, dict):
        mutable["extra"] = _redact_nested(dict(extra))
    request = mutable.get("request")
    if isinstance(request, dict):
        if isinstance(request.get("headers"), dict):
            request["headers"] = _redact_nested(request["headers"])
        if isinstance(request.get("data"), dict):
            request["data"] = _redact_nested(request["data"])
        if "cookies" in request:
            request["cookies"] = "***REDACTED***"
    return event


def _before_send_transaction(event: Event, hint: Hint) -> Event | None:  # noqa: ARG001
    return _before_send(event, hint)


def init_sentry(settings: Settings) -> None:
    """No-op until `OBSERVABILITY__SENTRY_DSN` is set — no provider credential
    exists in-app in Phase 1, so this is scaffolding, exercised for real starting
    whichever phase first configures a DSN.
    """
    dsn = settings.observability.sentry_dsn
    if dsn is None:
        return
    sentry_sdk.init(
        dsn=dsn.get_secret_value(),
        environment=settings.app_env.value,
        send_default_pii=False,
        before_send=_before_send,
        before_send_transaction=_before_send_transaction,
    )

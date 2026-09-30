"""Webhook HMAC verification — good / tampered / absent."""

from __future__ import annotations

import hashlib
import hmac

from app.features.social_accounts.service import verify_webhook_signature

_SECRET = "test-webhook-secret"  # noqa: S105
_BODY = b'{"id":"evt_1","type":"account.connected"}'


def _sig(body: bytes, secret: str = _SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_signature_good() -> None:
    assert (
        verify_webhook_signature(body=_BODY, signature=_sig(_BODY), secret=_SECRET)
        is True
    )


def test_webhook_signature_tampered() -> None:
    assert (
        verify_webhook_signature(
            body=_BODY, signature=_sig(b"tampered"), secret=_SECRET
        )
        is False
    )


def test_webhook_signature_absent() -> None:
    assert verify_webhook_signature(body=_BODY, signature=None, secret=_SECRET) is False
    assert verify_webhook_signature(body=_BODY, signature="", secret=_SECRET) is False

"""email.send job handler (architecture/12 §Backend components).

Payload schema — ids only, plus the raw token (which cannot be recovered from
its hash — see docs/phases/phase-03-background-jobs.md §Risks):

    reset:
        {"kind": "reset",
         "token_id": "<uuid>",
         "raw_token": "<str>"}

    invite / resend_invite:
        {"kind": "invite"|"resend_invite",
         "invitation_id": "<uuid>",
         "organization_id": "<uuid>",
         "raw_token": "<str>",
         "inviter_name": "<str>",
         "locale": "<str>",
         "resent_at": "<iso>"}       # resend_invite only

The handler re-reads the token/invitation row at run time so a stale payload
(an invitation revoked between enqueue and claim) is caught by the re-read.
The Resend idempotency-key is derived from stable business ids so a crash after
Resend accepts the send does not cause a double-send.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.features.auth import emails
from app.features.auth.repository import (
    get_invitation_by_id,
    get_user_by_id,
)
from app.features.organizations import get_organization
from app.integrations.email import get_email_provider
from app.jobs.errors import RetryableError, TerminalError

log = structlog.get_logger(__name__)


async def handle(payload: dict[str, Any]) -> None:
    """Entry point registered in REGISTRY['email.send']."""
    kind = payload.get("kind")
    if kind == "reset":
        await _handle_reset(payload)
    elif kind in ("invite", "resend_invite"):
        await _handle_invite(payload)
    else:
        raise TerminalError(f"EMAIL_SEND_UNKNOWN_KIND:{kind}")


async def _handle_reset(payload: dict[str, Any]) -> None:
    token_id = uuid.UUID(payload["token_id"])
    raw_token: str = payload["raw_token"]

    async with get_session_factory()() as session:
        from app.features.auth.models import PasswordResetToken

        token_row = await session.get(PasswordResetToken, token_id)
        if token_row is None or token_row.used_at is not None:
            log.info("email_send.reset_token_gone", token_id=str(token_id))
            return  # revoked or used — nothing to send, mark succeeded

        user = await get_user_by_id(session, token_row.user_id)
        if user is None:
            raise TerminalError("EMAIL_SEND_RESET_USER_NOT_FOUND")

        message = emails.password_reset_email(
            to=user.email,
            token=raw_token,
            locale=user.locale,
            idempotency_key=f"reset:{token_row.id}",
        )

    provider = get_email_provider(get_settings())
    try:
        await provider.send(message)
    except Exception as exc:
        log.warning("email_send.provider_error", exc=repr(exc))
        raise RetryableError() from exc

    log.info("email_send.reset_sent", token_id=str(token_id))


async def _handle_invite(payload: dict[str, Any]) -> None:
    invitation_id = uuid.UUID(payload["invitation_id"])
    organization_id = uuid.UUID(payload["organization_id"])
    raw_token: str = payload["raw_token"]
    inviter_name: str = payload["inviter_name"]
    locale: str = payload.get("locale", "ar")
    resent_at: str | None = payload.get("resent_at")

    async with get_session_factory()() as session:
        invitation = await get_invitation_by_id(
            session,
            organization_id=organization_id,
            invitation_id=invitation_id,
        )
        if invitation is None or invitation.revoked_at is not None:
            log.info("email_send.invitation_gone", invitation_id=str(invitation_id))
            return  # revoked — nothing to send

        org = await get_organization(session, organization_id)
        organization_name = org.name

    if resent_at:
        idempotency_key = f"invite-resend:{invitation_id}:{resent_at}"
    else:
        idempotency_key = f"invite:{invitation_id}"

    message = emails.invite_email(
        to=invitation.email,
        inviter_name=inviter_name,
        organization_name=organization_name,
        token=raw_token,
        locale=locale,
        idempotency_key=idempotency_key,
    )

    provider = get_email_provider(get_settings())
    try:
        await provider.send(message)
    except Exception as exc:
        log.warning("email_send.provider_error", exc=repr(exc))
        raise RetryableError() from exc

    log.info("email_send.invite_sent", invitation_id=str(invitation_id), kind=payload["kind"])

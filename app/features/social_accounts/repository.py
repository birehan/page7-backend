"""Persistence for social_accounts / Zernio tables (Phase 9)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.social_accounts.models import (
    SocialAccount,
    SocialAccountConnectAttempt,
    WebhookEvent,
    ZernioCredential,
    ZernioProfile,
)

PLATFORMS: tuple[str, ...] = (
    "instagram",
    "facebook",
    "tiktok",
    "snapchat",
    "whatsapp",
)


async def list_accounts_for_brand(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
) -> list[SocialAccount]:
    stmt = (
        select(SocialAccount)
        .where(
            SocialAccount.organization_id == organization_id,
            SocialAccount.brand_id == brand_id,
        )
        .order_by(SocialAccount.platform.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_account(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    account_id: uuid.UUID,
) -> SocialAccount | None:
    stmt = select(SocialAccount).where(
        SocialAccount.id == account_id,
        SocialAccount.organization_id == organization_id,
        SocialAccount.brand_id == brand_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_account_by_platform(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    platform: str,
) -> SocialAccount | None:
    stmt = select(SocialAccount).where(
        SocialAccount.organization_id == organization_id,
        SocialAccount.brand_id == brand_id,
        SocialAccount.platform == platform,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_account_by_zernio_id(
    session: AsyncSession,
    *,
    zernio_account_id: str,
) -> SocialAccount | None:
    stmt = select(SocialAccount).where(
        SocialAccount.zernio_account_id == zernio_account_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_placeholders(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
) -> list[SocialAccount]:
    """Insert one disconnected row per platform if missing (idempotent)."""
    existing = await list_accounts_for_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    have = {row.platform for row in existing}
    created: list[SocialAccount] = []
    for platform in PLATFORMS:
        if platform in have:
            continue
        row = SocialAccount(
            organization_id=organization_id,
            brand_id=brand_id,
            platform=platform,
            status="disconnected",
            handle="",
        )
        session.add(row)
        created.append(row)
    if created:
        await session.flush()
    return await list_accounts_for_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )


async def get_live_zernio_profile(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    credential_id: uuid.UUID | None = None,
) -> ZernioProfile | None:
    """Return one live profile for the brand.

    When ``credential_id`` is set, return that credential's pin. Otherwise
    return an arbitrary live profile (legacy callers / single-profile brands).
    Prefer ``list_live_zernio_profiles`` or ``get_live_zernio_profile_for_credential``
    for multi-key brands.
    """
    stmt = select(ZernioProfile).where(
        ZernioProfile.organization_id == organization_id,
        ZernioProfile.brand_id == brand_id,
        ZernioProfile.deleted_at.is_(None),
    )
    if credential_id is not None:
        stmt = stmt.where(ZernioProfile.credential_id == credential_id)
    stmt = stmt.order_by(ZernioProfile.created_at.asc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_live_zernio_profile_for_credential(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    credential_id: uuid.UUID,
) -> ZernioProfile | None:
    return await get_live_zernio_profile(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        credential_id=credential_id,
    )


async def list_live_zernio_profiles(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
) -> list[ZernioProfile]:
    stmt = (
        select(ZernioProfile)
        .where(
            ZernioProfile.organization_id == organization_id,
            ZernioProfile.brand_id == brand_id,
            ZernioProfile.deleted_at.is_(None),
        )
        .order_by(ZernioProfile.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def insert_zernio_profile(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    credential_id: uuid.UUID,
    zernio_profile_id: str,
) -> ZernioProfile:
    row = ZernioProfile(
        organization_id=organization_id,
        brand_id=brand_id,
        credential_id=credential_id,
        zernio_profile_id=zernio_profile_id,
        status="active",
    )
    session.add(row)
    await session.flush()
    return row


async def get_credential(
    session: AsyncSession, *, credential_id: uuid.UUID
) -> ZernioCredential | None:
    return await session.get(ZernioCredential, credential_id)


async def get_credential_by_alias(
    session: AsyncSession, *, alias: str
) -> ZernioCredential | None:
    stmt = select(ZernioCredential).where(ZernioCredential.alias == alias)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_active_credentials(session: AsyncSession) -> list[ZernioCredential]:
    stmt = select(ZernioCredential).where(ZernioCredential.status == "active")
    return list((await session.execute(stmt)).scalars().all())


async def list_live_profiles_for_credential(
    session: AsyncSession, *, credential_id: uuid.UUID
) -> list[ZernioProfile]:
    stmt = select(ZernioProfile).where(
        ZernioProfile.credential_id == credential_id,
        ZernioProfile.deleted_at.is_(None),
        ZernioProfile.status == "active",
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_connectedish_accounts(
    session: AsyncSession,
) -> list[SocialAccount]:
    """Accounts that participate in health sync."""
    stmt = select(SocialAccount).where(
        SocialAccount.status.in_(("connected", "expiring", "expired")),
        SocialAccount.zernio_account_id.is_not(None),
    )
    return list((await session.execute(stmt)).scalars().all())


async def insert_connect_attempt(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    social_account_id: uuid.UUID,
    kind: str,
    actor_user_id: uuid.UUID | None,
    oauth_state_hash: str | None = None,
    zernio_profile_id: uuid.UUID | None = None,
) -> SocialAccountConnectAttempt:
    row = SocialAccountConnectAttempt(
        organization_id=organization_id,
        brand_id=brand_id,
        social_account_id=social_account_id,
        kind=kind,
        actor_user_id=actor_user_id,
        oauth_state_hash=oauth_state_hash,
        zernio_profile_id=zernio_profile_id,
        status="started",
    )
    session.add(row)
    await session.flush()
    return row


async def finish_connect_attempt(
    session: AsyncSession,
    *,
    attempt: SocialAccountConnectAttempt,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    provider_response: dict[str, Any] | None = None,
) -> None:
    attempt.status = status
    attempt.error_code = error_code
    attempt.error_message = error_message
    attempt.provider_response = provider_response
    attempt.finished_at = utc_now()
    await session.flush()


async def mark_account_disconnected(
    session: AsyncSession,
    *,
    account: SocialAccount,
    clear_pins: bool = False,
) -> None:
    was_connected = (
        account.status in ("connected", "expiring", "expired")
        and account.zernio_account_id is not None
        and account.credential_id is not None
    )
    credential_id = account.credential_id
    account.status = "disconnected"
    account.zernio_account_id = None
    account.external_account_id = None
    account.handle = ""
    account.display_name = None
    account.avatar_url = None
    account.token_expires_at = None
    account.last_token_check_at = None
    account.scopes = None
    account.last_error_code = None
    account.last_error_at = None
    account.disconnected_at = utc_now()
    if clear_pins:
        account.zernio_profile_id = None
        account.credential_id = None
    await session.flush()
    if was_connected and credential_id is not None:
        await recompute_connected_accounts(session, credential_id=credential_id)


async def apply_connected_from_provider(
    session: AsyncSession,
    *,
    account: SocialAccount,
    zernio_account_id: str,
    handle: str,
    display_name: str | None,
    avatar_url: str | None,
    zernio_profile_id: uuid.UUID,
    credential_id: uuid.UUID,
    connected_by: uuid.UUID | None,
    token_expires_at: datetime | None = None,
) -> None:
    previous_credential_id = account.credential_id
    previously_counted = (
        account.status in ("connected", "expiring", "expired")
        and account.zernio_account_id is not None
        and account.credential_id is not None
    )
    account.zernio_account_id = zernio_account_id
    account.zernio_profile_id = zernio_profile_id
    account.credential_id = credential_id
    account.handle = handle
    account.display_name = display_name
    account.avatar_url = avatar_url
    account.status = "connected"
    account.token_expires_at = token_expires_at
    account.connected_at = account.connected_at or utc_now()
    account.disconnected_at = None
    account.connected_by = connected_by
    account.last_error_code = None
    account.last_error_at = None
    account.last_token_check_at = utc_now()
    await session.flush()
    if previously_counted and previous_credential_id is not None:
        await recompute_connected_accounts(
            session, credential_id=previous_credential_id
        )
    await recompute_connected_accounts(session, credential_id=credential_id)


async def insert_webhook_event(
    session: AsyncSession,
    *,
    provider: str,
    external_event_id: str,
    event_type: str,
    payload: dict[str, Any],
    signature_valid: bool,
) -> int | None:
    """Insert webhook event; return id if inserted, None if duplicate."""
    stmt = (
        insert(WebhookEvent)
        .values(
            provider=provider,
            external_event_id=external_event_id,
            event_type=event_type,
            payload=payload,
            signature_valid=signature_valid,
        )
        .on_conflict_do_nothing(index_elements=["provider", "external_event_id"])
        .returning(WebhookEvent.id)
    )
    result = await session.execute(stmt)
    row = result.first()
    return int(row[0]) if row else None


async def get_webhook_event(
    session: AsyncSession, *, webhook_event_id: int
) -> WebhookEvent | None:
    return await session.get(WebhookEvent, webhook_event_id)


async def mark_webhook_processed(
    session: AsyncSession,
    *,
    event: WebhookEvent,
    processing_error: str | None = None,
) -> None:
    event.processed_at = utc_now()
    event.attempts = (event.attempts or 0) + 1
    event.processing_error = processing_error
    await session.flush()


async def disable_credential(
    session: AsyncSession,
    *,
    credential: ZernioCredential,
    notes: str,
    auth: bool = False,
    payment: bool = False,
) -> None:
    now = utc_now()
    credential.status = "disabled"
    credential.notes = notes
    if auth:
        credential.last_401_at = now
    if payment:
        credential.last_402_at = now
    await session.flush()


async def mark_credential_at_capacity(
    session: AsyncSession,
    *,
    credential: ZernioCredential,
) -> None:
    """Zernio free-tier key is full — keep active but block new connects."""
    credential.connected_accounts = max(
        credential.connected_accounts, credential.max_accounts
    )
    credential.notes = "at_capacity"
    credential.last_402_at = utc_now()
    await session.flush()


async def enable_credential(
    session: AsyncSession,
    *,
    credential: ZernioCredential,
    notes: str | None = None,
) -> None:
    credential.status = "active"
    if notes is not None:
        credential.notes = notes
    await session.flush()


# Notes that mean "operator/test parked this key" — safe to revive when the
# alias is listed in ZERNIO_CREDENTIAL_ALIASES again. Auth/payment disables stay.
_REACTIVATABLE_NOTES = frozenset({"test_only", "at_capacity"})


async def ensure_credentials_from_env(
    session: AsyncSession,
    *,
    aliases: list[str],
    max_profiles: int = 100,
    max_accounts: int = 2,
) -> list[ZernioCredential]:
    """Upsert zernio_credentials rows for every configured alias (N-key pool).

    Configured aliases are revived when previously parked as ``test_only`` (or
    similar operator notes). Auth / hard payment disables are left alone.
    """
    rows: list[ZernioCredential] = []
    for alias in aliases:
        secret_ref = f"ZERNIO_API_KEY__{alias}"
        existing = await get_credential_by_alias(session, alias=alias)
        if existing is None:
            row = ZernioCredential(
                alias=alias,
                secret_ref=secret_ref,
                status="active",
                max_profiles=max_profiles,
                max_accounts=max_accounts,
            )
            session.add(row)
            await session.flush()
            rows.append(row)
            continue
        changed = False
        if existing.secret_ref != secret_ref:
            existing.secret_ref = secret_ref
            changed = True
        # Raise capacity from settings/env; never lower (paid-key overrides stick).
        if existing.max_accounts < max_accounts:
            existing.max_accounts = max_accounts
            changed = True
        if existing.max_profiles < max_profiles:
            existing.max_profiles = max_profiles
            changed = True
        if existing.status == "disabled" and _can_reactivate_credential(existing):
            existing.status = "active"
            if existing.notes in _REACTIVATABLE_NOTES:
                existing.notes = None
            changed = True
        if changed:
            await session.flush()
        rows.append(existing)
    return rows


def _can_reactivate_credential(credential: ZernioCredential) -> bool:
    """True when a disabled row was parked, not auth-/billing-killed."""
    if credential.last_401_at is not None:
        return False
    notes = (credential.notes or "").strip().lower()
    if notes in {"auth", "payment_required"}:
        return False
    if notes in _REACTIVATABLE_NOTES or notes == "":
        return True
    return False


async def set_connected_accounts(
    session: AsyncSession,
    *,
    credential: ZernioCredential,
    count: int,
) -> None:
    """Overwrite the Zernio account counter (live sync / capacity refresh)."""
    credential.connected_accounts = max(0, count)
    if (
        credential.notes == "at_capacity"
        and credential.connected_accounts < credential.max_accounts
    ):
        credential.notes = None
    await session.flush()


async def count_connected_accounts_for_credential(
    session: AsyncSession, *, credential_id: uuid.UUID
) -> int:
    stmt = select(func.count()).select_from(SocialAccount).where(
        SocialAccount.credential_id == credential_id,
        SocialAccount.status.in_(("connected", "expiring", "expired")),
        SocialAccount.zernio_account_id.is_not(None),
    )
    return int((await session.execute(stmt)).scalar_one())


async def recompute_connected_accounts(
    session: AsyncSession, *, credential_id: uuid.UUID
) -> int:
    count = await count_connected_accounts_for_credential(
        session, credential_id=credential_id
    )
    await session.execute(
        update(ZernioCredential)
        .where(ZernioCredential.id == credential_id)
        .values(connected_accounts=count)
    )
    await session.flush()
    return count


async def notification_exists_for_threshold(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    account_id: uuid.UUID,
    notif_type: str,
    days: int | None = None,
) -> bool:
    """Dedup helper — true if a matching notification already exists."""
    from sqlalchemy import text

    params: dict[str, str] = {
        "org_id": str(organization_id),
        "brand_id": str(brand_id),
        "type": notif_type,
        "account_id": str(account_id),
    }
    if days is not None:
        params["days"] = str(days)
        sql = """
            SELECT 1 FROM notifications
            WHERE organization_id = :org_id
              AND brand_id = :brand_id
              AND type = :type
              AND params->>'accountId' = :account_id
              AND params->>'days' = :days
            LIMIT 1
            """
    else:
        sql = """
            SELECT 1 FROM notifications
            WHERE organization_id = :org_id
              AND brand_id = :brand_id
              AND type = :type
              AND params->>'accountId' = :account_id
            LIMIT 1
            """
    result = await session.execute(text(sql), params)
    return result.first() is not None


async def touch_credential_health_check(
    session: AsyncSession, *, credential: ZernioCredential
) -> None:
    credential.last_health_check_at = utc_now()
    await session.flush()


async def bump_connected_accounts(
    session: AsyncSession, *, credential_id: uuid.UUID, delta: int
) -> None:
    await session.execute(
        update(ZernioCredential)
        .where(ZernioCredential.id == credential_id)
        .values(connected_accounts=ZernioCredential.connected_accounts + delta)
    )
    await session.flush()

"""Background tasks for social_accounts (health poll + webhook processing)."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.features.social_accounts import repository, service
from app.integrations.errors import ProviderError
from app.integrations.social import get_social_provider_for_credential
from app.jobs import queue as job_queue

log = structlog.get_logger(__name__)


async def run_zernio_health_poll(
    session: AsyncSession, *, settings: Settings | None = None
) -> None:
    cfg = settings or get_settings()
    credentials = await repository.list_active_credentials(session)
    for credential in credentials:
        if credential.status == "disabled":
            continue
        provider = get_social_provider_for_credential(
            cfg, alias=credential.alias, secret_ref=credential.secret_ref
        )
        profiles = await repository.list_live_profiles_for_credential(
            session, credential_id=credential.id
        )
        for profile in profiles:
            try:
                health_rows = await provider.get_accounts_health(
                    profile_id=profile.zernio_profile_id
                )
            except ProviderError:
                log.warning(
                    "zernio_health_poll.profile_failed",
                    alias=credential.alias,
                    profile_id=str(profile.id),
                )
                continue

            by_id = {h.account_id: h for h in health_rows}
            accounts = await repository.list_accounts_for_brand(
                session,
                organization_id=profile.organization_id,
                brand_id=profile.brand_id,
            )
            for account in accounts:
                if not account.zernio_account_id:
                    continue
                health = by_id.get(account.zernio_account_id)
                if health is None:
                    continue
                change = await service.apply_account_health(
                    session, account=account, health=health
                )
                if change is not None:
                    await service.maybe_notify_health(session, change=change)

        await repository.recompute_connected_accounts(
            session, credential_id=credential.id
        )
        await repository.touch_credential_health_check(session, credential=credential)


async def process_webhook_event(
    session: AsyncSession,
    *,
    webhook_event_id: int,
    alias: str | None = None,
    settings: Settings | None = None,
) -> None:
    cfg = settings or get_settings()
    event = await repository.get_webhook_event(
        session, webhook_event_id=webhook_event_id
    )
    if event is None:
        return
    if event.processed_at is not None:
        return

    event_type = event.event_type
    raw_payload = event.payload or {}
    payload: dict[str, Any] = dict(raw_payload) if isinstance(raw_payload, dict) else {}

    if event_type in ("account.connected", "account.disconnected"):
        await _apply_account_event(
            session, event_type=event_type, payload=payload, settings=cfg, alias=alias
        )
        await repository.mark_webhook_processed(session, event=event)
        return

    if event_type.startswith("post."):
        from app.features import publishing as publishing_feature

        await publishing_feature.apply_webhook_event(
            session,
            event_type=event_type,
            payload=payload,
            alias=alias,
        )
        await repository.mark_webhook_processed(session, event=event)
        return

    if event_type in ("comment.received", "message.received"):
        from app.features import inbox as inbox_feature

        await inbox_feature.apply_inbound_webhook(
            session, event_type=event_type, payload=payload
        )
        await repository.mark_webhook_processed(session, event=event)
        return

    if event_type == "conversation.started":
        from app.features import inbox as inbox_feature

        await inbox_feature.apply_conversation_started(session, payload=payload)
        await repository.mark_webhook_processed(session, event=event)
        return

    if event_type in (
        "message.sent",
        "message.delivered",
        "message.read",
        "message.failed",
    ):
        from app.features import inbox as inbox_feature

        await inbox_feature.apply_outbound_status_webhook(
            session, event_type=event_type, payload=payload
        )
        await repository.mark_webhook_processed(session, event=event)
        return

    if event_type == "analytics.synced":
        await _enqueue_analytics_sync_from_webhook(session, payload=payload)
        await repository.mark_webhook_processed(session, event=event)
        return

    await repository.mark_webhook_processed(
        session,
        event=event,
        processing_error="no handler for this event type yet",
    )


async def _enqueue_analytics_sync_from_webhook(
    session: AsyncSession, *, payload: dict[str, Any]
) -> None:
    raw_data = payload.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else payload
    account_obj = data.get("account")
    account_raw: dict[str, Any] = account_obj if isinstance(account_obj, dict) else {}
    zernio_account_id = str(
        data.get("accountId")
        or data.get("account_id")
        or account_raw.get("id")
        or ""
    )
    if not zernio_account_id:
        log.info("analytics.synced_missing_account")
        return
    account = await repository.get_account_by_zernio_id(
        session, zernio_account_id=zernio_account_id
    )
    if account is None or account.credential_id is None:
        log.info(
            "analytics.synced_unknown_account",
            zernio_account_id=zernio_account_id,
        )
        return
    await job_queue.enqueue(
        session,
        queue="sync",
        type="analytics_sync",
        payload={"credential_id": str(account.credential_id)},
        unique_key=f"analytics_sync_wh:{account.credential_id}",
        organization_id=account.organization_id,
    )


async def _apply_account_event(
    session: AsyncSession,
    *,
    event_type: str,
    payload: dict[str, Any],
    settings: Settings,
    alias: str | None,
) -> None:
    raw_data = payload.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else payload
    zernio_account_id = str(
        data.get("accountId") or data.get("id") or data.get("account_id") or ""
    )
    if not zernio_account_id:
        return

    account = await repository.get_account_by_zernio_id(
        session, zernio_account_id=zernio_account_id
    )
    if account is None:
        # Fallback: match by platform + profile if present
        platform = data.get("platform")
        profile_zernio_id = data.get("profileId") or data.get("profile_id")
        if platform and profile_zernio_id:
            from sqlalchemy import select

            from app.features.social_accounts.models import SocialAccount, ZernioProfile

            stmt = (
                select(SocialAccount)
                .join(
                    ZernioProfile,
                    SocialAccount.zernio_profile_id == ZernioProfile.id,
                )
                .where(
                    ZernioProfile.zernio_profile_id == str(profile_zernio_id),
                    SocialAccount.platform == str(platform),
                )
            )
            account = (await session.execute(stmt)).scalar_one_or_none()
        if account is None:
            log.info(
                "process_webhook.account_unknown",
                zernio_account_id=zernio_account_id,
                alias=alias,
            )
            return

    if event_type == "account.disconnected":
        previous = account.status
        await repository.mark_account_disconnected(
            session, account=account, clear_pins=False
        )
        if previous in ("connected", "expiring"):
            change = service.HealthStatusChange(
                account=account,
                previous_status=previous,
                new_status="disconnected",
                notify_disconnected=True,
                expiring_days=None,
            )
            await service.maybe_notify_health(session, change=change)
        return

    # account.connected
    handle = str(data.get("username") or data.get("handle") or account.handle or "")
    display_name = data.get("displayName") or data.get("display_name")
    avatar = data.get("profilePicture") or data.get("avatar_url")
    if account.zernio_profile_id is None or account.credential_id is None:
        profile = await repository.get_live_zernio_profile(
            session,
            organization_id=account.organization_id,
            brand_id=account.brand_id,
        )
        if profile is None:
            return
        zernio_profile_id = profile.id
        credential_id = profile.credential_id
    else:
        zernio_profile_id = account.zernio_profile_id
        credential_id = account.credential_id

    await repository.apply_connected_from_provider(
        session,
        account=account,
        zernio_account_id=zernio_account_id,
        handle=handle,
        display_name=str(display_name) if display_name else None,
        avatar_url=str(avatar) if avatar else None,
        zernio_profile_id=zernio_profile_id,
        credential_id=credential_id,
        connected_by=account.connected_by,
    )
    from app.features import inbox as inbox_feature

    await inbox_feature.enqueue_inbox_sync(
        session,
        social_account_id=account.id,
        organization_id=account.organization_id,
    )
    _ = settings  # reserved for future credential-side effects

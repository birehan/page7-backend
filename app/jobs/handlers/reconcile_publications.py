"""reconcile_publications — resume stuck sent/accepted publications (architecture/10 §7)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog

from app.core.config import Settings, get_settings
from app.core.time import utc_now
from app.db.session import get_session_factory
from app.features.publishing import apply_publish_result, mark_outcome_unknown, repository
from app.features.publishing.exceptions import NonDefinitiveOutcome
from app.features.social_accounts import get_account, get_credential
from app.integrations.social import get_social_provider_for_credential
from app.integrations.social.ports import PublishRequest

log = structlog.get_logger(__name__)

SENT_RESUME_AFTER = timedelta(minutes=3)
ACCEPTED_POLL_AFTER = timedelta(minutes=5)
OUTCOME_UNKNOWN_AFTER = timedelta(minutes=30)


async def handle(payload: dict[str, Any]) -> None:
    del payload  # cron — empty payload
    factory = get_session_factory()
    settings = get_settings()
    now = utc_now()

    async with factory() as session:
        sent_rows = list(
            await repository.list_sent_needing_resume(
                session, older_than=now - SENT_RESUME_AFTER
            )
        )
        accepted_rows = list(
            await repository.list_accepted_needing_poll(
                session, older_than=now - ACCEPTED_POLL_AFTER
            )
        )
        unknown_rows = list(
            await repository.list_unresolved_past_deadline(
                session, older_than=now - OUTCOME_UNKNOWN_AFTER
            )
        )
        await session.commit()

    unknown_ids = {row.id for row in unknown_rows}
    for pub in unknown_rows:
        async with factory() as session:
            try:
                await mark_outcome_unknown(session, pub.id)
                await session.commit()
                log.warning(
                    "reconcile.outcome_unknown",
                    publication_id=str(pub.id),
                    post_id=str(pub.post_id),
                )
            except Exception:
                log.exception(
                    "reconcile.outcome_unknown_failed",
                    publication_id=str(pub.id),
                )
                await session.rollback()

    for pub in sent_rows:
        if pub.id in unknown_ids:
            continue
        async with factory() as session:
            live = await repository.has_any_live_publish_job_for_post(
                session, post_id=pub.post_id
            )
            await session.commit()
        if live:
            continue
        await _resume_sent(pub_id=pub.id, settings=settings)

    for pub in accepted_rows:
        if pub.id in unknown_ids:
            continue
        if not pub.zernio_post_id:
            continue
        await _poll_accepted(pub_id=pub.id, settings=settings)


async def _resume_sent(*, pub_id: Any, settings: Settings) -> None:
    factory = get_session_factory()
    async with factory() as session:
        pub = await repository.get_publication(session, pub_id)
        if pub is None or pub.status != "sent":
            return
        account = await get_account(
            session,
            organization_id=pub.organization_id,
            brand_id=pub.brand_id,
            account_id=pub.social_account_id,
        )
        if account is None or account.credential_id is None:
            return
        credential = await get_credential(session, credential_id=account.credential_id)
        if credential is None:
            return
        if not pub.request_payload:
            log.warning(
                "reconcile.resume_missing_payload",
                publication_id=str(pub.id),
            )
            return
        request = PublishRequest.model_validate(pub.request_payload)
        provider = get_social_provider_for_credential(
            settings, alias=credential.alias, secret_ref=credential.secret_ref
        )
        await session.commit()

    result = await provider.publish(request)
    async with factory() as session:
        try:
            await apply_publish_result(session, pub_id, result)
            await session.commit()
        except NonDefinitiveOutcome:
            await session.commit()
        except Exception:
            log.exception("reconcile.resume_apply_failed", publication_id=str(pub_id))
            await session.rollback()


async def _poll_accepted(*, pub_id: Any, settings: Settings) -> None:
    factory = get_session_factory()
    async with factory() as session:
        pub = await repository.get_publication(session, pub_id)
        if pub is None or pub.status != "accepted" or not pub.zernio_post_id:
            return
        account = await get_account(
            session,
            organization_id=pub.organization_id,
            brand_id=pub.brand_id,
            account_id=pub.social_account_id,
        )
        if account is None or account.credential_id is None:
            return
        credential = await get_credential(session, credential_id=account.credential_id)
        if credential is None:
            return
        provider = get_social_provider_for_credential(
            settings, alias=credential.alias, secret_ref=credential.secret_ref
        )
        zernio_post_id = pub.zernio_post_id
        await session.commit()

    result = await provider.get_post(zernio_post_id)
    async with factory() as session:
        try:
            await apply_publish_result(session, pub_id, result)
            await session.commit()
        except NonDefinitiveOutcome:
            await session.commit()
        except Exception:
            log.exception("reconcile.poll_apply_failed", publication_id=str(pub_id))
            await session.rollback()

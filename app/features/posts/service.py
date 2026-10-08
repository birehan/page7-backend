from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.time import riyadh_date_key, riyadh_datetime_to_utc, riyadh_today, utc_now
from app.features import audit, brands
from app.features import media as media_feature
from app.features import notifications as notifications_feature
from app.features import organizations as orgs_feature
from app.features.posts import repository
from app.features.posts.models import Post, PostComment, PostVersion
from app.features.posts.risk_engine import compute_risk, riyadh_wall_time
from app.features.posts.schemas import (
    AddCommentBody,
    BulkApproveItem,
    CreatePostBody,
    DisclosureIn,
    PostCommentOut,
    PostMediaIn,
    PostMediaOut,
    PostOut,
    PostVariantIn,
    PostVersionOut,
    PostVersionSnapshotOut,
    PublishErrorOut,
    ReasonBody,
    RescheduleResponse,
    RiskReportOut,
    UpdatePostBody,
)
from app.integrations.storage.ports import ObjectStorage
from app.jobs import queue as job_queue

EDITABLE_STATUSES = frozenset({"draft", "changes_requested"})

# Bounded job attempts for the real publish pipeline (architecture/10 / Phase 10).
PUBLISH_POST_MAX_ATTEMPTS = 10

POST_STATUSES = (
    "idea",
    "drafting",
    "draft",
    "in_review",
    "changes_requested",
    "approved",
    "scheduled",
    "publishing",
    "published",
    "failed",
)

# Mirrors pgblank-web/src/features/posts/utils.ts TRANSITIONS exactly.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "idea": frozenset({"drafting"}),
    "drafting": frozenset({"draft"}),
    "draft": frozenset({"in_review"}),
    "in_review": frozenset({"approved", "changes_requested", "draft"}),
    "changes_requested": frozenset({"drafting", "draft", "in_review"}),
    "approved": frozenset({"scheduled"}),
    "scheduled": frozenset({"publishing", "draft"}),
    "publishing": frozenset({"published", "failed"}),
    "published": frozenset({"draft"}),
    "failed": frozenset({"scheduled", "draft"}),
}

POLICY_ACTOR_REF = "system:approval-policy"
POLICY_ACTOR_NAME = "Approval policy"


@dataclass(frozen=True)
class Actor:
    name: str
    ref: str
    kind: Literal["user", "system", "policy", "guest"]
    user_id: uuid.UUID | None = None


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in LEGAL_TRANSITIONS.get(from_status, frozenset())


def policy_actor() -> Actor:
    return Actor(
        name=POLICY_ACTOR_NAME,
        ref=POLICY_ACTOR_REF,
        kind="policy",
        user_id=None,
    )


def user_actor(*, user_id: uuid.UUID, name: str) -> Actor:
    return Actor(name=name, ref=str(user_id), kind="user", user_id=user_id)


def guest_actor(*, review_link_id: uuid.UUID, name: str) -> Actor:
    """Guest reviewer — ref is always `guest:<review_link_id>`."""
    return Actor(
        name=f"{name} (guest)",
        ref=f"guest:{review_link_id}",
        kind="guest",
        user_id=None,
    )


def _caption_snippet(variants: list[dict[str, Any]] | list[PostVariantIn]) -> str:
    raw: list[dict[str, Any]]
    if variants and isinstance(variants[0], PostVariantIn):
        raw = [v.model_dump(by_alias=False) for v in variants]
    else:
        raw = [dict(v) for v in variants]
    ar = next((v for v in raw if v.get("lang") == "ar"), None)
    en = next((v for v in raw if v.get("lang") == "en"), None)
    caption = str((ar or en or {}).get("caption", ""))
    return f"{caption[:60]}…" if len(caption) > 60 else caption


def _variants_dicts(variants: list[PostVariantIn] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not variants:
        return []
    if isinstance(variants[0], PostVariantIn):
        return [
            v.model_dump(by_alias=False, exclude_none=True)
            for v in variants
        ]
    return [dict(v) for v in variants]


async def _assert_brand(
    session: AsyncSession, *, organization_id: uuid.UUID, brand_id: uuid.UUID
) -> Any:
    brand = await brands.get_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)
    return brand


def _banned_claims(brand: Any) -> list[str]:
    guidelines = getattr(brand, "guidelines", None)
    if guidelines is None:
        return []
    claims = getattr(guidelines, "banned_claims", None)
    if claims is None and isinstance(guidelines, dict):
        claims = guidelines.get("bannedClaims") or guidelines.get("banned_claims")
    return list(claims or [])


def _brand_city(brand: Any) -> str | None:
    return getattr(brand, "city", None)


async def _resolve_media_items(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    media: list[PostMediaIn] | None,
) -> list[tuple[uuid.UUID, int, str | None, None]]:
    if not media:
        return []
    asset_ids: list[uuid.UUID] = []
    alts: list[str | None] = []
    for item in media:
        raw = item.library_id or item.id
        try:
            asset_ids.append(uuid.UUID(raw))
        except ValueError as exc:
            raise ApiError(
                "VALIDATION",
                f"Invalid media library id: {raw}",
                status_code=422,
            ) from exc
        alts.append(item.alt or None)

    assets = await media_feature.get_assets_by_ids(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        asset_ids=asset_ids,
    )
    by_id = {a.id: a for a in assets}
    missing = [aid for aid in asset_ids if aid not in by_id]
    if missing:
        raise ApiError(
            "VALIDATION",
            "One or more media assets were not found on this brand",
            status_code=422,
            details={"missing": [str(m) for m in missing]},
        )
    for asset in assets:
        if asset.kind not in ("image", "video"):
            raise ApiError(
                "VALIDATION",
                "Only image and video assets can be attached to a post",
                status_code=422,
            )
    return [
        (asset_ids[i], i, alts[i], None) for i in range(len(asset_ids))
    ]


async def _media_outs(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    storage: ObjectStorage,
    primary_lang: str = "ar",
) -> list[PostMediaOut]:
    rows = list(await repository.list_post_media(session, post_id=post_id))
    if not rows:
        return []
    asset_ids = [r.media_asset_id for r in rows]
    assets = await media_feature.get_assets_by_ids(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        asset_ids=asset_ids,
    )
    by_id = {a.id: a for a in assets}
    out: list[PostMediaOut] = []
    for row in rows:
        asset = by_id.get(row.media_asset_id)
        if asset is None:
            continue
        kind = "video" if asset.kind == "video" else "image"
        if row.alt_override is not None and row.alt_override != "":
            alt = row.alt_override
        elif primary_lang == "en":
            alt = asset.alt_en or asset.alt_ar
        else:
            alt = asset.alt_ar or asset.alt_en
        out.append(
            PostMediaOut(
                id=str(row.id),
                kind=kind,
                url=storage.public_url(asset.r2_key),
                alt=alt,
                library_id=str(asset.id),
            )
        )
    return out


async def _media_alts_for_risk(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    media_items: list[tuple[uuid.UUID, int, str | None, None]] | None,
    existing_post_id: uuid.UUID | None = None,
) -> list[str] | None:
    if media_items is not None:
        if not media_items:
            return []
        asset_ids = [m[0] for m in media_items]
        assets = await media_feature.get_assets_by_ids(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            asset_ids=asset_ids,
        )
        by_id = {a.id: a for a in assets}
        alts: list[str] = []
        for asset_id, _pos, alt_override, _crop in media_items:
            if alt_override is not None:
                alts.append(alt_override)
            else:
                asset = by_id.get(asset_id)
                alts.append((asset.alt_ar or asset.alt_en) if asset else "")
        return alts
    if existing_post_id is None:
        return None
    rows = list(await repository.list_post_media(session, post_id=existing_post_id))
    if not rows:
        return []
    assets = await media_feature.get_assets_by_ids(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        asset_ids=[r.media_asset_id for r in rows],
    )
    by_id = {a.id: a for a in assets}
    result: list[str] = []
    for row in rows:
        if row.alt_override is not None:
            result.append(row.alt_override)
        else:
            asset = by_id.get(row.media_asset_id)
            result.append((asset.alt_ar or asset.alt_en) if asset else "")
    return result


async def post_to_out(
    session: AsyncSession,
    post: Post,
    *,
    storage: ObjectStorage,
) -> PostOut:
    media = await _media_outs(
        session,
        organization_id=post.organization_id,
        brand_id=post.brand_id,
        post_id=post.id,
        storage=storage,
    )
    last_error = None
    if post.last_error is not None:
        last_error = PublishErrorOut.model_validate(post.last_error)
    return PostOut(
        id=post.id,
        organization_id=post.organization_id,
        brand_id=post.brand_id,
        group_id=post.group_id,
        title=post.title,
        internal_note=post.internal_note,
        pillar_id=post.pillar_id,
        platform=post.platform,
        status=post.status,
        disclosure=DisclosureIn(is_paid=post.is_paid),
        scheduled_at=post.scheduled_at,
        first_comment=post.first_comment,
        published_at=post.published_at,
        submitted_at=post.submitted_at,
        approved_at=post.approved_at,
        variants=[PostVariantIn.model_validate(v) for v in (post.variants or [])],
        media=media,
        risk=RiskReportOut.model_validate(post.risk),
        cultural_event_id=post.cultural_event_id,
        change_request_reason=post.change_request_reason,
        reject_reason=post.reject_reason,
        last_error=last_error,
        created_by=post.created_by,
        approved_by=post.approved_by_user_id,
        ai_decision_id=post.ai_decision_id,
        version=post.version,
        created_at=post.created_at,
        updated_at=post.updated_at,
    )


def _version_snapshot_dict(out: PostOut) -> dict[str, Any]:
    return {
        "variants": [v.model_dump(by_alias=True, exclude_none=True) for v in out.variants],
        "media": [m.model_dump(by_alias=True, exclude_none=True) for m in out.media],
        "scheduledAt": out.scheduled_at.isoformat().replace("+00:00", "Z"),
        "platform": out.platform,
        "pillarId": str(out.pillar_id) if out.pillar_id else None,
    }


async def _snapshot(
    session: AsyncSession,
    post: Post,
    *,
    reason: str,
    actor: Actor,
    storage: ObjectStorage,
) -> PostVersion:
    out = await post_to_out(session, post, storage=storage)
    version_num = await repository.next_version_number(session, post_id=post.id)
    return await repository.insert_post_version(
        session,
        organization_id=post.organization_id,
        brand_id=post.brand_id,
        post_id=post.id,
        version=version_num,
        post_row_version=post.version,
        reason=reason,
        author_user_id=actor.user_id,
        author_ref=actor.ref,
        author_name=actor.name,
        snapshot=_version_snapshot_dict(out),
        brand_version=post.brand_version,
    )


async def transition(
    session: AsyncSession,
    post: Post,
    to: str,
    actor: Actor,
    *,
    patch: dict[str, Any] | None = None,
    snapshot_reason: str | None = None,
    audit_action: str | None = None,
    storage: ObjectStorage,
) -> Post:
    """Single choke point for every status-changing action."""
    if not can_transition(post.status, to):
        raise ApiError(
            "INVALID_TRANSITION",
            f"Cannot transition from {post.status} to {to}",
            status_code=409,
        )

    if to in ("scheduled", "publishing"):
        settings = await orgs_feature.get_org_settings(session, post.organization_id)
        if settings.publishing_frozen:
            raise ApiError(
                "FROZEN",
                "Publishing is frozen for this organization.",
                status_code=409,
            )

    expected_version = post.version
    from_status = post.status
    updated = await repository.cas_transition(
        session,
        post_id=post.id,
        brand_id=post.brand_id,
        from_status=from_status,
        expected_version=expected_version,
        to_status=to,
        patch=patch,
    )
    if updated is None:
        existing = await repository.get_post(
            session,
            organization_id=post.organization_id,
            brand_id=post.brand_id,
            post_id=post.id,
        )
        if existing is None:
            raise ApiError("NOT_FOUND", "Post not found", status_code=404)
        if existing.status != from_status:
            raise ApiError(
                "INVALID_TRANSITION",
                f"Cannot transition from {existing.status} to {to}",
                status_code=409,
            )
        raise ApiError(
            "VERSION_CONFLICT",
            "Post was modified by someone else",
            status_code=409,
            details={"currentVersion": existing.version},
        )

    if snapshot_reason is not None:
        await _snapshot(
            session, updated, reason=snapshot_reason, actor=actor, storage=storage
        )

    await audit.record(
        session,
        organization_id=updated.organization_id,
        brand_id=updated.brand_id,
        actor_kind=actor.kind,
        actor_ref=actor.ref,
        actor_name=actor.name,
        actor_user_id=actor.user_id,
        action=audit_action or f"post.{to}",
        target_type="post",
        target_id=updated.id,
    )
    return updated


async def _cancel_publish_job(
    session: AsyncSession, *, post_id: uuid.UUID, epoch: int
) -> None:
    await session.execute(
        sa.text(
            "DELETE FROM jobs WHERE unique_key = :key AND state = 'queued'"
        ),
        {"key": f"publish:{post_id}:{epoch}"},
    )


async def _enqueue_publish_post(
    session: AsyncSession,
    post: Post,
    *,
    epoch: int,
    trigger: str = "scheduled",
    priority: int | None = None,
    run_at: datetime | None = None,
) -> None:
    """Enqueue publish_post. Priority: 10 scheduled, 5 publish_now/retry/auto_retry."""
    if priority is None:
        priority = 10 if trigger == "scheduled" else 5
    due = run_at if run_at is not None else post.scheduled_at
    await job_queue.enqueue(
        session,
        queue="publishing",
        type="publish_post",
        payload={
            "post_id": str(post.id),
            "epoch": epoch,
            "organization_id": str(post.organization_id),
            "trigger": trigger,
            "run_at": due.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        },
        unique_key=f"publish:{post.id}:{epoch}",
        run_at=due,
        priority=priority,
        max_attempts=PUBLISH_POST_MAX_ATTEMPTS,
        organization_id=post.organization_id,
    )


async def _is_publishing_frozen(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> bool:
    settings = await orgs_feature.get_org_settings(session, organization_id)
    return bool(settings.publishing_frozen)


async def _enter_scheduled(
    session: AsyncSession,
    post: Post,
    actor: Actor,
    *,
    storage: ObjectStorage,
    audit_action: str = "post.scheduled",
    trigger: str = "scheduled",
    priority: int | None = None,
    run_at: datetime | None = None,
    scheduled_at: datetime | None = None,
) -> Post:
    """Bump schedule_epoch, CAS to scheduled, enqueue publish_post in same tx."""
    from app.features.publishing.service import assert_media_publishable

    await assert_media_publishable(session, post)

    patch: dict[str, Any] = {"schedule_epoch": post.schedule_epoch + 1}
    if scheduled_at is not None:
        patch["scheduled_at"] = scheduled_at
    new_epoch = int(patch["schedule_epoch"])
    updated = await transition(
        session,
        post,
        "scheduled",
        actor,
        patch=patch,
        audit_action=audit_action,
        storage=storage,
    )
    await _enqueue_publish_post(
        session,
        updated,
        epoch=new_epoch,
        trigger=trigger,
        priority=priority,
        run_at=run_at,
    )
    return updated


async def list_posts(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    storage: ObjectStorage,
    status: str | None = None,
    platform: str | None = None,
    scheduled_from: datetime | None = None,
    scheduled_to: datetime | None = None,
) -> list[PostOut]:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    posts = await repository.list_posts(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        status=status,
        platform=platform,
        scheduled_from=scheduled_from,
        scheduled_to=scheduled_to,
    )
    return [await post_to_out(session, p, storage=storage) for p in posts]


async def get_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    storage: ObjectStorage,
) -> PostOut | None:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        return None
    return await post_to_out(session, post, storage=storage)


async def create_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    body: CreatePostBody,
    actor: Actor,
    storage: ObjectStorage,
) -> PostOut:
    brand = await _assert_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if actor.user_id is None:
        raise ApiError("VALIDATION", "A user actor is required", status_code=422)

    media_items = await _resolve_media_items(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        media=body.media,
    )
    variants = _variants_dicts(body.variants)
    media_alts = await _media_alts_for_risk(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        media_items=media_items,
    )
    is_paid = bool(body.disclosure and body.disclosure.is_paid)
    risk = compute_risk(
        variants=variants,
        platforms=[body.platform],
        banned_claims=_banned_claims(brand),
        media_alts=media_alts,
        scheduled_at=body.scheduled_at,
        city=_brand_city(brand),
        is_paid=is_paid,
    )
    post = await repository.insert_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        created_by=actor.user_id,
        platform=body.platform,
        scheduled_at=body.scheduled_at,
        variants=variants,
        risk=risk.to_dict(),
        risk_score=Decimal(str(risk.score)),
        is_paid=is_paid,
        first_comment=body.first_comment,
        pillar_id=body.pillar_id,
        cultural_event_id=body.cultural_event_id,
        group_id=body.group_id,
        ai_decision_id=body.ai_decision_id,
    )
    if media_items:
        await repository.replace_post_media(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            post_id=post.id,
            items=media_items,
        )
    await _snapshot(session, post, reason="created", actor=actor, storage=storage)
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind=actor.kind,
        actor_ref=actor.ref,
        actor_name=actor.name,
        actor_user_id=actor.user_id,
        action="post.created",
        target_type="post",
        target_id=post.id,
    )
    return await post_to_out(session, post, storage=storage)


async def update_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    body: UpdatePostBody,
    actor: Actor,
    storage: ObjectStorage,
) -> PostOut:
    brand = await _assert_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    if post.status not in EDITABLE_STATUSES:
        raise ApiError(
            "INVALID_TRANSITION",
            f"Cannot edit a post in status {post.status}",
            status_code=409,
        )

    expected_version = body.version if body.version is not None else post.version
    platform = body.platform or post.platform
    variants = (
        _variants_dicts(body.variants)
        if body.variants is not None
        else list(post.variants or [])
    )
    scheduled_at = body.scheduled_at or post.scheduled_at
    is_paid = (
        body.disclosure.is_paid
        if body.disclosure is not None
        else post.is_paid
    )

    media_items: list[tuple[uuid.UUID, int, str | None, None]] | None = None
    if body.media is not None:
        media_items = await _resolve_media_items(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            media=body.media,
        )

    media_alts = await _media_alts_for_risk(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        media_items=media_items,
        existing_post_id=post.id if media_items is None else None,
    )
    risk = compute_risk(
        variants=variants,
        platforms=[platform],
        banned_claims=_banned_claims(brand),
        media_alts=media_alts,
        scheduled_at=scheduled_at,
        city=_brand_city(brand),
        is_paid=is_paid,
    )

    values: dict[str, Any] = {
        "platform": platform,
        "variants": variants,
        "scheduled_at": scheduled_at,
        "is_paid": is_paid,
        "risk": risk.to_dict(),
        "risk_score": Decimal(str(risk.score)),
    }
    if body.first_comment is not None:
        values["first_comment"] = body.first_comment
    if body.pillar_id is not None:
        values["pillar_id"] = body.pillar_id
    if body.cultural_event_id is not None:
        values["cultural_event_id"] = body.cultural_event_id
    if body.internal_note is not None:
        values["internal_note"] = body.internal_note

    updated = await repository.cas_update_content(
        session,
        post_id=post.id,
        brand_id=brand_id,
        expected_version=expected_version,
        allowed_statuses=list(EDITABLE_STATUSES),
        values=values,
    )
    if updated is None:
        existing = await repository.get_post(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            post_id=post_id,
        )
        if existing is None:
            raise ApiError("NOT_FOUND", "Post not found", status_code=404)
        if existing.status not in EDITABLE_STATUSES:
            raise ApiError(
                "INVALID_TRANSITION",
                f"Cannot edit a post in status {existing.status}",
                status_code=409,
            )
        raise ApiError(
            "VERSION_CONFLICT",
            "Post was modified by someone else",
            status_code=409,
            details={"currentVersion": existing.version},
        )

    if media_items is not None:
        await repository.replace_post_media(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            post_id=updated.id,
            items=media_items,
        )

    await _snapshot(session, updated, reason="edited", actor=actor, storage=storage)
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind=actor.kind,
        actor_ref=actor.ref,
        actor_name=actor.name,
        actor_user_id=actor.user_id,
        action="post.updated",
        target_type="post",
        target_id=updated.id,
    )
    return await post_to_out(session, updated, storage=storage)


async def duplicate_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    actor: Actor,
    storage: ObjectStorage,
) -> PostOut:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    if actor.user_id is None:
        raise ApiError("VALIDATION", "A user actor is required", status_code=422)
    source = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if source is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)

    # Deliberate correction over the mock: clear group_id on duplicate.
    copy = await repository.insert_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        created_by=actor.user_id,
        platform=source.platform,
        scheduled_at=source.scheduled_at,
        variants=list(source.variants or []),
        risk=dict(source.risk or {"score": 0, "reasons": []}),
        risk_score=source.risk_score,
        is_paid=source.is_paid,
        first_comment=source.first_comment,
        pillar_id=source.pillar_id,
        cultural_event_id=source.cultural_event_id,
        group_id=None,
        ai_decision_id=None,
        title=source.title,
        internal_note=source.internal_note,
    )
    source_media = list(await repository.list_post_media(session, post_id=source.id))
    if source_media:
        await repository.replace_post_media(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            post_id=copy.id,
            items=[
                (m.media_asset_id, m.position, m.alt_override, m.crop)
                for m in source_media
            ],
        )
    await _snapshot(session, copy, reason="created", actor=actor, storage=storage)
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind=actor.kind,
        actor_ref=actor.ref,
        actor_name=actor.name,
        actor_user_id=actor.user_id,
        action="post.duplicated",
        target_type="post",
        target_id=copy.id,
        meta={"sourcePostId": str(post_id)},
    )
    return await post_to_out(session, copy, storage=storage)


async def get_post_group(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    storage: ObjectStorage,
) -> list[PostOut]:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    if post.group_id is None:
        siblings = [post]
    else:
        siblings = list(
            await repository.list_group(
                session,
                organization_id=organization_id,
                brand_id=brand_id,
                group_id=post.group_id,
            )
        )
    return [await post_to_out(session, p, storage=storage) for p in siblings]


async def list_versions(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
) -> list[PostVersionOut]:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    rows = await repository.list_versions(session, post_id=post.id)
    result: list[PostVersionOut] = []
    for row in rows:
        if row.author_user_id is None:
            continue
        snap = row.snapshot or {}
        result.append(
            PostVersionOut(
                id=row.id,
                post_id=row.post_id,
                version=row.version,
                reason=row.reason,
                author_id=row.author_user_id,
                author_name=row.author_name,
                created_at=row.created_at,
                snapshot=PostVersionSnapshotOut.model_validate(snap),
            )
        )
    return result


def _comment_out(comment: PostComment) -> PostCommentOut:
    return PostCommentOut(
        id=comment.id,
        post_id=comment.post_id,
        author_id=comment.author_user_id,
        author_name=comment.author_name,
        body=comment.body,
        lang=comment.lang,
        resolved=comment.resolved_at is not None,
        created_at=comment.created_at,
    )


async def list_comments(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
) -> list[PostCommentOut]:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    rows = await repository.list_comments(session, post_id=post.id)
    return [_comment_out(c) for c in rows]


async def add_comment(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    body: AddCommentBody,
    actor: Actor,
) -> PostCommentOut:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    if actor.user_id is None:
        raise ApiError("VALIDATION", "A user actor is required", status_code=422)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    comment = await repository.insert_comment(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post.id,
        author_user_id=actor.user_id,
        author_name=actor.name,
        body=body.body,
        lang=body.lang,
    )
    return _comment_out(comment)


async def resolve_comment(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    comment_id: uuid.UUID,
    actor: Actor,
) -> PostCommentOut:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    if actor.user_id is None:
        raise ApiError("VALIDATION", "A user actor is required", status_code=422)
    comment = await repository.get_comment(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        comment_id=comment_id,
    )
    if comment is None:
        raise ApiError("NOT_FOUND", "Comment not found", status_code=404)
    if comment.resolved_at is None:
        comment = await repository.resolve_comment(
            session, comment=comment, resolved_by=actor.user_id
        )
    return _comment_out(comment)


async def list_approval_queue(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    storage: ObjectStorage,
) -> list[PostOut]:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    posts = await repository.list_approval_queue(
        session, organization_id=organization_id, brand_id=brand_id
    )
    return [await post_to_out(session, p, storage=storage) for p in posts]


async def _can_auto_approve(
    session: AsyncSession, *, organization_id: uuid.UUID, risk_score: float
) -> bool:
    settings = await orgs_feature.get_org_settings(session, organization_id)
    if settings.approval_mode != "autonomous":
        return False
    if settings.publishing_frozen:
        return False
    return float(risk_score) <= float(settings.risk_threshold)


async def submit_for_review(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    actor: Actor,
    storage: ObjectStorage,
) -> PostOut:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)

    updated = await transition(
        session,
        post,
        "in_review",
        actor,
        patch={"submitted_at": utc_now()},
        snapshot_reason="edited",
        audit_action="post.submitted",
        storage=storage,
    )

    if await _can_auto_approve(
        session,
        organization_id=organization_id,
        risk_score=float(updated.risk_score),
    ):
        policy = policy_actor()
        approved = await transition(
            session,
            updated,
            "approved",
            policy,
            patch={
                "approved_at": utc_now(),
                "approved_by_user_id": None,
                "approved_via": "policy",
            },
            audit_action="post.auto_approved",
            storage=storage,
        )
        await notifications_feature.fan_out(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            type="post_approved",
            message_key="post_auto_approved",
            params={"title": _caption_snippet(list(approved.variants or []))},
            target_href=f"/posts/{approved.id}",
            actor_user_id=None,
            actor_ref=POLICY_ACTOR_REF,
            recipient_user_ids=[approved.created_by],
        )
        scheduled = await _enter_scheduled(
            session, approved, policy, storage=storage
        )
        return await post_to_out(session, scheduled, storage=storage)

    await notifications_feature.fan_out(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        type="approval_requested",
        message_key="approval_requested",
        params={
            "actor": actor.name,
            "title": _caption_snippet(list(updated.variants or [])),
        },
        target_href="/approvals",
        actor_user_id=actor.user_id,
        actor_ref=actor.ref,
    )
    return await post_to_out(session, updated, storage=storage)


async def resubmit(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    actor: Actor,
    storage: ObjectStorage,
) -> PostOut:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)

    updated = await transition(
        session,
        post,
        "in_review",
        actor,
        patch={"submitted_at": utc_now()},
        snapshot_reason="changes_applied",
        audit_action="post.resubmitted",
        storage=storage,
    )
    await notifications_feature.fan_out(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        type="approval_requested",
        message_key="approval_requested",
        params={
            "actor": actor.name,
            "title": _caption_snippet(list(updated.variants or [])),
        },
        target_href="/approvals",
        actor_user_id=actor.user_id,
        actor_ref=actor.ref,
    )
    return await post_to_out(session, updated, storage=storage)


async def withdraw(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    actor: Actor,
    storage: ObjectStorage,
) -> PostOut:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    updated = await transition(
        session,
        post,
        "draft",
        actor,
        audit_action="post.withdrawn",
        storage=storage,
    )
    return await post_to_out(session, updated, storage=storage)


async def request_changes(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    body: ReasonBody,
    actor: Actor,
    storage: ObjectStorage,
) -> PostOut:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    updated = await transition(
        session,
        post,
        "changes_requested",
        actor,
        patch={"change_request_reason": body.reason},
        audit_action="post.changes_requested",
        storage=storage,
    )
    await notifications_feature.fan_out(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        type="changes_requested",
        message_key="changes_requested",
        params={
            "actor": actor.name,
            "title": _caption_snippet(list(updated.variants or [])),
        },
        target_href=f"/posts/{updated.id}",
        actor_user_id=actor.user_id,
        actor_ref=actor.ref,
        recipient_user_ids=[updated.created_by],
    )
    return await post_to_out(session, updated, storage=storage)


async def reject_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    body: ReasonBody,
    actor: Actor,
    storage: ObjectStorage,
) -> PostOut:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    updated = await transition(
        session,
        post,
        "draft",
        actor,
        patch={"reject_reason": body.reason},
        audit_action="post.rejected",
        storage=storage,
    )
    await notifications_feature.fan_out(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        type="post_rejected",
        message_key="post_rejected",
        params={
            "title": _caption_snippet(list(updated.variants or [])),
            "reason": body.reason,
        },
        target_href=f"/posts/{updated.id}",
        actor_user_id=actor.user_id,
        actor_ref=actor.ref,
        recipient_user_ids=[updated.created_by],
    )
    return await post_to_out(session, updated, storage=storage)


async def approve_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    actor: Actor,
    storage: ObjectStorage,
) -> PostOut:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)

    approved = await transition(
        session,
        post,
        "approved",
        actor,
        patch={
            "approved_at": utc_now(),
            "approved_by_user_id": actor.user_id,
            "approved_via": "user",
        },
        snapshot_reason="edited",
        audit_action="post.approved",
        storage=storage,
    )
    await notifications_feature.fan_out(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        type="post_approved",
        message_key="post_approved",
        params={"title": _caption_snippet(list(approved.variants or []))},
        target_href=f"/posts/{approved.id}",
        actor_user_id=actor.user_id,
        actor_ref=actor.ref,
        recipient_user_ids=[approved.created_by],
    )

    # Frozen: stop at approved (silent). Direct /schedule while frozen is 409.
    if await _is_publishing_frozen(session, organization_id=organization_id):
        return await post_to_out(session, approved, storage=storage)

    scheduled = await _enter_scheduled(
        session, approved, actor, storage=storage
    )
    return await post_to_out(session, scheduled, storage=storage)


async def approve_many(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_ids: list[uuid.UUID],
    actor: Actor,
    storage: ObjectStorage,
) -> list[BulkApproveItem]:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    results: list[BulkApproveItem] = []
    for post_id in post_ids:
        try:
            await approve_post(
                session,
                organization_id=organization_id,
                brand_id=brand_id,
                post_id=post_id,
                actor=actor,
                storage=storage,
            )
            results.append(BulkApproveItem(id=post_id, ok=True))
        except ApiError:
            results.append(BulkApproveItem(id=post_id, ok=False))
    return results


async def approve_via_review_link(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    review_link_id: uuid.UUID,
    actor: Actor,
    storage: ObjectStorage,
) -> bool:
    """Approve one frozen sibling if still `in_review`. Returns False when skipped."""
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None or post.status != "in_review":
        return False

    approved = await transition(
        session,
        post,
        "approved",
        actor,
        patch={
            "approved_at": utc_now(),
            "approved_by_user_id": None,
            "approved_via": "review_link",
            "approved_review_link_id": review_link_id,
        },
        snapshot_reason="edited",
        audit_action="post.approved",
        storage=storage,
    )
    if not await _is_publishing_frozen(session, organization_id=organization_id):
        await _enter_scheduled(session, approved, actor, storage=storage)
    return True


async def request_changes_via_review_link(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    reason: str,
    actor: Actor,
    storage: ObjectStorage,
) -> bool:
    """Request changes on one frozen sibling if still `in_review`. Returns False when skipped."""
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None or post.status != "in_review":
        return False

    await transition(
        session,
        post,
        "changes_requested",
        actor,
        patch={"change_request_reason": reason},
        audit_action="post.changes_requested",
        storage=storage,
    )
    return True


async def schedule_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    actor: Actor,
    storage: ObjectStorage,
) -> PostOut:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    # transition() enforces FROZEN for →scheduled
    scheduled = await _enter_scheduled(session, post, actor, storage=storage)
    return await post_to_out(session, scheduled, storage=storage)


async def unschedule_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    actor: Actor,
    storage: ObjectStorage,
) -> PostOut:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)

    epoch = post.schedule_epoch
    updated = await transition(
        session,
        post,
        "draft",
        actor,
        audit_action="post.unscheduled",
        storage=storage,
    )
    await _cancel_publish_job(session, post_id=updated.id, epoch=epoch)
    return await post_to_out(session, updated, storage=storage)


async def reschedule_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    date_key: str,
    actor: Actor,
    storage: ObjectStorage,
) -> RescheduleResponse:
    await _assert_brand(session, organization_id=organization_id, brand_id=brand_id)
    post = await repository.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    if post.status != "scheduled":
        raise ApiError(
            "INVALID_TRANSITION",
            f"Cannot reschedule a post in status {post.status}",
            status_code=409,
        )

    wall = riyadh_wall_time(post.scheduled_at)
    new_scheduled_at = riyadh_datetime_to_utc(date_key, wall)
    old_epoch = post.schedule_epoch
    new_epoch = old_epoch + 1
    from_iso = post.scheduled_at

    updated = await repository.cas_update_content(
        session,
        post_id=post.id,
        brand_id=brand_id,
        expected_version=post.version,
        allowed_statuses=["scheduled"],
        values={
            "scheduled_at": new_scheduled_at,
            "schedule_epoch": new_epoch,
        },
    )
    if updated is None:
        existing = await repository.get_post(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            post_id=post_id,
        )
        if existing is None:
            raise ApiError("NOT_FOUND", "Post not found", status_code=404)
        if existing.status != "scheduled":
            raise ApiError(
                "INVALID_TRANSITION",
                f"Cannot reschedule a post in status {existing.status}",
                status_code=409,
            )
        raise ApiError(
            "VERSION_CONFLICT",
            "Post was modified by someone else",
            status_code=409,
            details={"currentVersion": existing.version},
        )

    await _cancel_publish_job(session, post_id=updated.id, epoch=old_epoch)
    await _enqueue_publish_post(session, updated, epoch=new_epoch)

    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind=actor.kind,
        actor_ref=actor.ref,
        actor_name=actor.name,
        actor_user_id=actor.user_id,
        action="post.rescheduled",
        target_type="post",
        target_id=updated.id,
        meta={
            "from": from_iso.isoformat().replace("+00:00", "Z"),
            "to": updated.scheduled_at.isoformat().replace("+00:00", "Z"),
        },
    )

    warnings: list[Literal["conflict", "past"]] = []
    if riyadh_date_key(updated.scheduled_at) < riyadh_today().isoformat():
        warnings.append("past")

    siblings = await repository.list_posts(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        platform=updated.platform,
    )
    window = timedelta(hours=1)
    for other in siblings:
        if other.id == updated.id:
            continue
        delta = abs(other.scheduled_at - updated.scheduled_at)
        if delta < window:
            warnings.append("conflict")
            break

    out = await post_to_out(session, updated, storage=storage)
    return RescheduleResponse(post=out, warnings=warnings)


async def soft_delete_for_brand(
    session: AsyncSession, *, brand_id: uuid.UUID, deleted_by: uuid.UUID
) -> int:
    """Cascade soft-delete when a brand is deleted (Phase 4 forward obligation)."""
    return await repository.soft_delete_brand_posts(
        session, brand_id=brand_id, deleted_by=deleted_by
    )


async def media_in_use(
    session: AsyncSession, *, media_asset_id: uuid.UUID
) -> bool:
    """True when the asset is on a scheduled/publishing post (MEDIA_IN_USE guard)."""
    return await repository.media_referenced_by_active_posts(
        session, media_asset_id=media_asset_id
    )

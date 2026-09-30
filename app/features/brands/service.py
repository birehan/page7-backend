from __future__ import annotations

import secrets
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.ids import new_uuid7
from app.features import audit
from app.features.brands import repository
from app.features.brands.industry_defaults import (
    DEFAULT_CREATE_GUIDELINES,
    guidelines_from_defaults,
    lookup_industry_defaults,
)
from app.features.brands.models import Brand, BrandCompetitor, ContentPillar
from app.features.brands.schemas import (
    BrandGuidelinesOut,
    BrandOut,
    CompetitorOut,
    ContentPillarIn,
    ContentPillarOut,
    CreateBrandBody,
    GenerateBrandBrainBody,
    IngestLogoBody,
    UpdateBrandBody,
    guidelines_to_dict,
)
from app.features.media.service import (
    MAX_IMAGE_BYTES,
    probe_image_dimensions,
    validate_declared,
)
from app.integrations.storage.ports import ObjectStorage
from app.integrations.webfetch.ssrf import safe_connect


def _pillar_out(pillar: ContentPillar) -> ContentPillarOut:
    return ContentPillarOut(
        id=pillar.id,
        name=pillar.name,
        description=pillar.description,
        weight=float(pillar.weight),
    )


def _competitor_out(competitor: BrandCompetitor) -> CompetitorOut:
    return CompetitorOut(
        id=competitor.id, handle=competitor.handle, platform=competitor.platform
    )


def brand_to_out(
    brand: Brand,
    pillars: list[ContentPillar],
    competitors: list[BrandCompetitor],
) -> BrandOut:
    return BrandOut(
        id=brand.id,
        organization_id=brand.organization_id,
        name=brand.name,
        industry=brand.industry,
        city=brand.city,
        website=brand.website,
        logo_url=brand.logo_url,
        guidelines=BrandGuidelinesOut.model_validate(brand.guidelines),
        pillars=[_pillar_out(p) for p in pillars],
        competitors=[_competitor_out(c) for c in competitors],
        version=brand.version,
        updated_at=brand.updated_at,
        created_at=brand.created_at,
    )


def _snapshot_dict(out: BrandOut) -> dict[str, Any]:
    return out.model_dump(mode="json", by_alias=True)


async def _load_out(session: AsyncSession, brand: Brand) -> BrandOut:
    pillars = list(await repository.list_pillars(session, brand_id=brand.id))
    competitors = list(await repository.list_competitors(session, brand_id=brand.id))
    return brand_to_out(brand, pillars, competitors)


async def list_brands(session: AsyncSession, *, organization_id: uuid.UUID) -> list[BrandOut]:
    brands = await repository.list_brands(session, organization_id=organization_id)
    result: list[BrandOut] = []
    for brand in brands:
        result.append(await _load_out(session, brand))
    return result


async def get_brand(
    session: AsyncSession, *, organization_id: uuid.UUID, brand_id: uuid.UUID
) -> BrandOut | None:
    brand = await repository.get_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if brand is None:
        return None
    return await _load_out(session, brand)


async def create_brand(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    body: CreateBrandBody,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> BrandOut:
    from app.features import billing as billing_feature
    from app.features import organizations as orgs_feature

    await orgs_feature.lock_organization_row(session, organization_id)
    await billing_feature.enforce_brand_limit(
        session, organization_id=organization_id
    )

    name = body.name.strip()
    if not name:
        raise ApiError("VALIDATION", "A brand name is required", status_code=422)

    try:
        brand = await repository.insert_brand(
            session,
            organization_id=organization_id,
            name=name,
            industry=body.industry.strip(),
            city=body.city.strip(),
            website=body.website.strip() if body.website else None,
            guidelines=dict(DEFAULT_CREATE_GUIDELINES),
        )
    except IntegrityError as exc:
        raise ApiError(
            "CONFLICT", "A brand with that name already exists", status_code=409
        ) from exc

    await repository.insert_pillar(
        session,
        organization_id=organization_id,
        brand_id=brand.id,
        name=name,
        description="",
        weight=3,
        position=0,
    )

    from app.features import social_accounts as social_accounts_feature

    await social_accounts_feature.create_placeholders_for_brand(
        session, organization_id=organization_id, brand_id=brand.id
    )

    out = await _load_out(session, brand)
    await repository.insert_brand_version(
        session,
        organization_id=organization_id,
        brand_id=brand.id,
        version=brand.version,
        reason="edited",
        author_user_id=actor_user_id,
        snapshot=_snapshot_dict(out),
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand.id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="brand.created",
        target_type="brand",
        target_id=brand.id,
    )
    return out


def diff_pillars(
    existing: list[ContentPillar],
    incoming: list[ContentPillarIn],
) -> tuple[
    list[ContentPillarIn],
    list[tuple[ContentPillar, ContentPillarIn, int]],
    list[uuid.UUID],
]:
    """Return (to_insert, to_update_with_position, to_soft_delete_ids)."""
    existing_by_id = {p.id: p for p in existing}
    incoming_ids = {p.id for p in incoming}
    to_delete = [p.id for p in existing if p.id not in incoming_ids]
    to_insert: list[ContentPillarIn] = []
    to_update: list[tuple[ContentPillar, ContentPillarIn, int]] = []
    for position, pillar in enumerate(incoming):
        if pillar.id in existing_by_id:
            to_update.append((existing_by_id[pillar.id], pillar, position))
        else:
            to_insert.append(pillar)
    # Assign positions for inserts via their index in incoming — handled by caller.
    return to_insert, to_update, to_delete


async def update_brand(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    body: UpdateBrandBody,
    actor_user_id: uuid.UUID,
    actor_name: str,
    if_match: str | None = None,
) -> BrandOut:
    if body.id != brand_id or body.organization_id != organization_id:
        raise ApiError("VALIDATION", "Brand id/organization mismatch", status_code=422)

    if if_match is not None:
        expected = if_match.strip().strip('"')
        if expected != str(body.version):
            raise ApiError(
                "VALIDATION",
                "If-Match disagrees with body.version",
                status_code=422,
            )

    name = body.name.strip()
    if not name:
        raise ApiError("VALIDATION", "A brand name is required", status_code=422)

    try:
        updated = await repository.cas_update_brand(
            session,
            brand_id=brand_id,
            organization_id=organization_id,
            expected_version=body.version,
            name=name,
            industry=body.industry.strip(),
            city=body.city.strip(),
            website=body.website,
            logo_url=body.logo_url,
            guidelines=guidelines_to_dict(body.guidelines),
        )
    except IntegrityError as exc:
        raise ApiError(
            "CONFLICT", "A brand with that name already exists", status_code=409
        ) from exc

    if updated is None:
        existing = await repository.get_brand_including_deleted(
            session, organization_id=organization_id, brand_id=brand_id
        )
        if existing is None or existing.deleted_at is not None:
            raise ApiError("NOT_FOUND", "Brand not found", status_code=404)
        raise ApiError(
            "VERSION_CONFLICT",
            "Brand was modified by someone else",
            status_code=409,
            details={"currentVersion": existing.version},
        )

    existing_pillars = list(await repository.list_pillars(session, brand_id=brand_id))
    to_insert, to_update, to_delete = diff_pillars(existing_pillars, body.pillars)

    if to_delete:
        await repository.soft_delete_pillars(
            session, brand_id=brand_id, deleted_by=actor_user_id, pillar_ids=to_delete
        )

    for pillar, incoming, position in to_update:
        pillar.name = incoming.name
        pillar.description = incoming.description
        pillar.weight = incoming.weight  # type: ignore[assignment]
        pillar.position = position

    incoming_positions = {p.id: i for i, p in enumerate(body.pillars)}
    for incoming in to_insert:
        await repository.insert_pillar(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            name=incoming.name,
            description=incoming.description,
            weight=incoming.weight,
            position=incoming_positions[incoming.id],
            pillar_id=incoming.id,
        )

    await repository.replace_competitors(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        competitors=[(c.id, c.handle, c.platform) for c in body.competitors],
    )

    out = await _load_out(session, updated)
    await repository.insert_brand_version(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        version=updated.version,
        reason="edited",
        author_user_id=actor_user_id,
        snapshot=_snapshot_dict(out),
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="brand.updated",
        target_type="brand",
        target_id=brand_id,
    )
    return out


async def generate_brand_defaults(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    body: GenerateBrandBrainBody,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> BrandOut:
    brand = await repository.get_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)

    defaults = lookup_industry_defaults(body.industry)
    guidelines = guidelines_from_defaults(defaults, dialect=body.dialect)

    # CAS against current version so a concurrent edit still conflicts cleanly.
    try:
        updated = await repository.cas_update_brand(
            session,
            brand_id=brand_id,
            organization_id=organization_id,
            expected_version=brand.version,
            name=body.name.strip(),
            industry=body.industry.strip(),
            city=body.city.strip(),
            website=body.website.strip() if body.website else None,
            logo_url=brand.logo_url,
            guidelines=guidelines,
        )
    except IntegrityError as exc:
        raise ApiError(
            "CONFLICT", "A brand with that name already exists", status_code=409
        ) from exc

    if updated is None:
        existing = await repository.get_brand(
            session, organization_id=organization_id, brand_id=brand_id
        )
        if existing is None:
            raise ApiError("NOT_FOUND", "Brand not found", status_code=404)
        raise ApiError(
            "VERSION_CONFLICT",
            "Brand was modified by someone else",
            status_code=409,
            details={"currentVersion": existing.version},
        )

    # Replace pillars: soft-delete all live ones, insert the industry defaults.
    await repository.soft_delete_pillars(
        session, brand_id=brand_id, deleted_by=actor_user_id
    )
    await repository.delete_all_competitors(session, brand_id=brand_id)

    for position, pillar in enumerate(defaults.pillars):
        await repository.insert_pillar(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            name=pillar["name"],
            description=pillar["description"],
            weight=pillar["weight"],
            position=position,
            pillar_id=new_uuid7(),
        )

    out = await _load_out(session, updated)
    await repository.insert_brand_version(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        version=updated.version,
        reason="generated",
        author_user_id=actor_user_id,
        snapshot=_snapshot_dict(out),
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="brand.generated",
        target_type="brand",
        target_id=brand_id,
    )
    return out


async def delete_brand(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> None:
    """Soft-delete a brand and cascade to pillars, media_assets, posts, and social_accounts."""
    brand = await repository.get_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)

    await repository.soft_delete_pillars(
        session, brand_id=brand_id, deleted_by=actor_user_id
    )
    from app.core.config import get_settings
    from app.features import media as media_feature
    from app.features import posts as posts_feature
    from app.features import social_accounts as social_accounts_feature

    await media_feature.soft_delete_for_brand(
        session, brand_id=brand_id, deleted_by=actor_user_id
    )
    await posts_feature.soft_delete_for_brand(
        session, brand_id=brand_id, deleted_by=actor_user_id
    )
    await social_accounts_feature.soft_delete_for_brand(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        settings=get_settings(),
    )
    await repository.soft_delete_brand(session, brand=brand, deleted_by=actor_user_id)
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="brand.deleted",
        target_type="brand",
        target_id=brand_id,
    )


def _logo_storage_key(organization_id: uuid.UUID, brand_id: uuid.UUID) -> str:
    token = secrets.token_hex(16)
    return f"orgs/{organization_id}/brands/{brand_id}/logo/{token}/original"


async def ingest_logo(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    body: IngestLogoBody,
    actor_user_id: uuid.UUID,
    actor_name: str,
    storage: ObjectStorage,
) -> BrandOut:
    """Fetch a remote logo SSRF-safely into the public bucket; set brands.logo_url."""
    brand = await repository.get_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)
    if brand.version != body.expected_version:
        raise ApiError(
            "VERSION_CONFLICT",
            "Brand was modified by someone else",
            status_code=409,
            details={"currentVersion": brand.version},
        )

    try:
        response = await safe_connect(body.source_url, max_bytes=MAX_IMAGE_BYTES)
    except Exception as exc:
        raise ApiError(
            "VALIDATION",
            "Could not fetch logo from the provided URL",
            status_code=422,
            details={"reason": str(exc)[:200]},
        ) from exc

    content_type = (response.content_type or "application/octet-stream").split(";")[0].strip()
    if not content_type.startswith("image/"):
        # Fall back to PIL detection — some CDNs omit or mislabel Content-Type.
        content_type = "image/png"
    validate_declared(content_type=content_type, size=len(response.content))
    probe_image_dimensions(response.content)

    key = _logo_storage_key(organization_id, brand_id)
    await storage.put_object("public", key, response.content, content_type=content_type)
    logo_url = storage.public_url(key)

    updated = await repository.cas_update_logo_url(
        session,
        brand_id=brand_id,
        organization_id=organization_id,
        expected_version=body.expected_version,
        logo_url=logo_url,
    )
    if updated is None:
        raise ApiError(
            "VERSION_CONFLICT",
            "Brand was modified by someone else",
            status_code=409,
        )

    out = await _load_out(session, updated)
    await repository.insert_brand_version(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        version=updated.version,
        reason="edited",
        author_user_id=actor_user_id,
        snapshot=_snapshot_dict(out),
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="brand.logo_ingested",
        target_type="brand",
        target_id=brand_id,
        meta={"sourceUrl": body.source_url, "logoUrl": logo_url},
    )
    return out

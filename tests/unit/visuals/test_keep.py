"""Unit tests for keep — FakeObjectStorage asserts copy + delete."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ids import new_uuid7
from app.features.auth.models import User
from app.features.brands.industry_defaults import DEFAULT_CREATE_GUIDELINES
from app.features.brands.models import Brand
from app.features.organizations.models import Organization
from app.features.visuals import keep_render, repository
from app.features.visuals.models import ImageGeneration
from app.features.visuals.schemas import KeepVisualBody
from app.integrations.storage.fakes import FakeObjectStorage


@pytest.mark.asyncio
async def test_keep_copies_and_deletes_pending(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email=f"k{uuid.uuid4().hex[:8]}@ex.com", name="K")
    org = Organization(name="KOrg")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="KB",
        industry="retail",
        city="Riyadh",
        website="https://x.sa",
        guidelines=dict(DEFAULT_CREATE_GUIDELINES),
    )
    db_session.add(brand)
    await db_session.flush()

    gen_id = new_uuid7()
    gen = ImageGeneration(
        id=gen_id,
        organization_id=org.id,
        brand_id=brand.id,
        requested_by=user.id,
        model="fal-ai/flux/dev",
        prompt="coffee",
        style="photo",
        aspect="square",
        count=1,
    )
    db_session.add(gen)
    await db_session.flush()

    pending_key = f"gen/pending/{gen_id}/0"
    output = await repository.insert_output(
        db_session,
        organization_id=org.id,
        image_generation_id=gen_id,
        index=0,
        provider_url="https://fal.media/files/fake/0.png",
        provider_url_expires_at=datetime.now(UTC) + timedelta(days=7),
        width=1024,
        height=1024,
        seed=1,
        r2_key=pending_key,
    )
    await db_session.commit()

    storage = FakeObjectStorage(public_base_url="https://media.test")
    await storage.put_object("public", pending_key, b"png-bytes", content_type="image/png")

    monkeypatch.setattr(
        "app.features.visuals.keep_render.get_object_storage",
        lambda settings: storage,
    )

    settings = get_settings()
    body = KeepVisualBody.model_validate(
        {
            "brandId": str(brand.id),
            "url": f"https://media.test/{pending_key}",
            "prompt": "coffee",
            "style": "photo",
            "altAr": "قهوة",
            "altEn": "coffee",
        }
    )
    result = await keep_render.keep_visual(
        db_session,
        organization_id=org.id,
        user_id=user.id,
        body=body,
        settings=settings,
    )

    assert result["source"] == "generated"
    assert ("public", pending_key) not in storage.objects
    assert any(k.startswith("orgs/") for (_, k) in storage.objects)
    assert ("public", pending_key) in [(b, k) for b, k in storage.deleted] or (
        "public",
        pending_key,
    ) in storage.deleted
    await db_session.refresh(output)
    assert output.kept_at is not None


@pytest.mark.asyncio
async def test_keep_is_idempotent_when_already_kept(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email=f"k{uuid.uuid4().hex[:8]}@ex.com", name="K")
    org = Organization(name="KOrg2")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="KB2",
        industry="retail",
        city="Riyadh",
        website="https://x.sa",
        guidelines=dict(DEFAULT_CREATE_GUIDELINES),
    )
    db_session.add(brand)
    await db_session.flush()

    gen_id = new_uuid7()
    gen = ImageGeneration(
        id=gen_id,
        organization_id=org.id,
        brand_id=brand.id,
        requested_by=user.id,
        model="fal-ai/flux/dev",
        prompt="coffee",
        style="photo",
        aspect="square",
        count=1,
    )
    db_session.add(gen)
    await db_session.flush()

    pending_key = f"gen/pending/{gen_id}/0"
    await repository.insert_output(
        db_session,
        organization_id=org.id,
        image_generation_id=gen_id,
        index=0,
        provider_url="https://fal.media/files/fake/0.png",
        provider_url_expires_at=datetime.now(UTC) + timedelta(days=7),
        width=1024,
        height=1024,
        seed=1,
        r2_key=pending_key,
    )
    await db_session.commit()

    storage = FakeObjectStorage(public_base_url="https://media.test")
    await storage.put_object("public", pending_key, b"png-bytes", content_type="image/png")
    monkeypatch.setattr(
        "app.features.visuals.keep_render.get_object_storage",
        lambda settings: storage,
    )

    settings = get_settings()
    body = KeepVisualBody.model_validate(
        {
            "brandId": str(brand.id),
            "url": f"https://media.test/{pending_key}",
            "prompt": "coffee",
            "style": "photo",
            "altAr": "قهوة",
            "altEn": "coffee",
        }
    )
    first = await keep_render.keep_visual(
        db_session,
        organization_id=org.id,
        user_id=user.id,
        body=body,
        settings=settings,
    )
    second = await keep_render.keep_visual(
        db_session,
        organization_id=org.id,
        user_id=user.id,
        body=body,
        settings=settings,
    )
    assert second["id"] == first["id"]
    assert second["source"] == "generated"

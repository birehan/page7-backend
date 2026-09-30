"""Persistence for image_generations / image_generation_outputs."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.visuals.models import ImageGeneration, ImageGenerationOutput


async def create_generation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    requested_by: uuid.UUID,
    model: str,
    prompt: str,
    style: str,
    aspect: str,
    count: int,
    use_brand_colors: bool,
    negative_prompt: str | None = None,
    generation_id: uuid.UUID | None = None,
) -> ImageGeneration:
    row = ImageGeneration(
        organization_id=organization_id,
        brand_id=brand_id,
        requested_by=requested_by,
        model=model,
        prompt=prompt,
        negative_prompt=negative_prompt,
        style=style,
        aspect=aspect,
        count=count,
        use_brand_colors=use_brand_colors,
        status="queued",
    )
    if generation_id is not None:
        row.id = generation_id
    session.add(row)
    await session.flush()
    return row


async def get_generation(
    session: AsyncSession, *, generation_id: uuid.UUID
) -> ImageGeneration | None:
    return (
        await session.execute(
            select(ImageGeneration).where(ImageGeneration.id == generation_id)
        )
    ).scalar_one_or_none()


async def mark_generation_running(
    session: AsyncSession, generation: ImageGeneration
) -> None:
    if generation.status == "queued":
        generation.status = "running"
        await session.flush()


async def finalize_generation(
    session: AsyncSession,
    generation: ImageGeneration,
    *,
    status: str,
    decision_id: uuid.UUID | None,
    cost_usd: Decimal | None,
    latency_ms: int | None,
    error_code: str | None = None,
    provider_request_id: str | None = None,
) -> None:
    generation.status = status
    generation.decision_id = decision_id
    generation.cost_usd = cost_usd
    generation.latency_ms = latency_ms
    generation.error_code = error_code
    generation.provider_request_id = provider_request_id
    generation.finished_at = utc_now()
    await session.flush()


async def insert_output(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    image_generation_id: uuid.UUID,
    index: int,
    provider_url: str,
    provider_url_expires_at: datetime | None,
    width: int,
    height: int,
    seed: int | None,
    r2_key: str | None = None,
) -> ImageGenerationOutput:
    row = ImageGenerationOutput(
        organization_id=organization_id,
        image_generation_id=image_generation_id,
        index=index,
        provider_url=provider_url,
        provider_url_expires_at=provider_url_expires_at,
        width=width,
        height=height,
        seed=seed,
        r2_key=r2_key,
    )
    session.add(row)
    await session.flush()
    return row


async def set_output_r2_key(
    session: AsyncSession,
    output: ImageGenerationOutput,
    *,
    r2_key: str,
) -> None:
    output.r2_key = r2_key
    await session.flush()


async def get_output_by_r2_key(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    r2_key: str,
) -> ImageGenerationOutput | None:
    return (
        await session.execute(
            select(ImageGenerationOutput).where(
                ImageGenerationOutput.organization_id == organization_id,
                ImageGenerationOutput.r2_key == r2_key,
            )
        )
    ).scalar_one_or_none()


async def mark_output_kept(
    session: AsyncSession, output: ImageGenerationOutput
) -> None:
    output.kept_at = utc_now()
    await session.flush()

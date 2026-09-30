"""ai.plan_commit — persist accepted plan draft items as posts."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.db import import_models as _import_models  # noqa: F401
from app.db.session import get_session_factory
from app.features import audit, posts
from app.features.content_ai import repository
from app.features.content_ai.schemas import PlanDraftItemIn
from app.features.posts.schemas import CreatePostBody
from app.integrations.storage import get_object_storage
from app.jobs.errors import TerminalError
from app.sse.bridge import emit, mark_run_finished, mark_run_running
from app.sse.models import Run

log = __import__("structlog").get_logger(__name__)


async def handle(payload: dict[str, Any]) -> None:
    run_id_raw = payload.get("run_id")
    if not isinstance(run_id_raw, str):
        raise TerminalError("PLAN_COMMIT_BAD_PAYLOAD")
    try:
        run_id = uuid.UUID(run_id_raw)
        organization_id = uuid.UUID(str(payload["organization_id"]))
        brand_id = uuid.UUID(str(payload["brand_id"]))
        user_id = uuid.UUID(str(payload["user_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise TerminalError("PLAN_COMMIT_BAD_PAYLOAD") from exc

    user_name = str(payload.get("user_name") or "User")
    items_raw = payload.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise TerminalError("PLAN_COMMIT_BAD_PAYLOAD")

    parent_raw = payload.get("parent_decision_id")
    parent_decision_id: uuid.UUID | None = None
    if parent_raw:
        try:
            parent_decision_id = uuid.UUID(str(parent_raw))
        except ValueError as exc:
            raise TerminalError("PLAN_COMMIT_BAD_PAYLOAD") from exc

    settings = get_settings()
    storage = get_object_storage(settings)
    factory = get_session_factory()

    async with factory() as session:
        run = (
            await session.execute(select(Run).where(Run.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            await session.commit()
            return
        if run.status == "succeeded":
            await session.commit()
            return
        await mark_run_running(session, run)
        await session.commit()

    actor = posts.user_actor(user_id=user_id, name=user_name)
    total = len(items_raw)
    created_posts: list[dict[str, Any]] = []

    try:
        for index, raw in enumerate(items_raw):
            item = PlanDraftItemIn.model_validate(raw)
            body = CreatePostBody(
                platform=item.platform,
                scheduled_at=item.scheduled_at,
                variants=item.variants,
                pillar_id=item.pillar_id,
                cultural_event_id=item.cultural_event_id,
                ai_decision_id=parent_decision_id,
            )
            async with factory() as session:
                post_out = await posts.create_post(
                    session,
                    organization_id=organization_id,
                    brand_id=brand_id,
                    body=body,
                    actor=actor,
                    storage=storage,
                )
                await emit(
                    session,
                    run_id,
                    {
                        "type": "post_created",
                        "post": post_out.model_dump(mode="json", by_alias=True),
                        "index": index,
                        "total": total,
                    },
                )
                created_posts.append(post_out.model_dump(mode="json", by_alias=True))
                await session.commit()

        async with factory() as session:
            decision = await repository.insert_decision(
                session,
                organization_id=organization_id,
                brand_id=brand_id,
                actor_user_id=user_id,
                kind="plan_commit",
                provider="internal",
                model="internal",
                prompt_version="plan-commit-v1",
                inputs_hash=str(run_id),
                input_summary={"itemCount": total, "runId": str(run_id)},
                output={"items": items_raw, "posts": created_posts},
                status="succeeded",
                target_type="brand",
                target_id=brand_id,
                parent_decision_id=parent_decision_id,
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=Decimal("0"),
            )
            await audit.record(
                session,
                organization_id=organization_id,
                brand_id=brand_id,
                actor_kind="user",
                actor_ref=str(user_id),
                actor_name=user_name,
                actor_user_id=user_id,
                action="ai.plan_committed",
                target_type="ai_decision",
                target_id=decision.id,
                meta={"itemCount": total},
            )
            await emit(
                session,
                run_id,
                {"type": "done", "decisionId": str(decision.id)},
            )
            run = (
                await session.execute(select(Run).where(Run.id == run_id))
            ).scalar_one()
            await mark_run_finished(
                session,
                run,
                status="succeeded",
                result={"decisionId": str(decision.id), "postCount": total},
            )
            await session.commit()
    except Exception as exc:
        async with factory() as session:
            run = (
                await session.execute(select(Run).where(Run.id == run_id))
            ).scalar_one_or_none()
            if run is not None:
                await emit(
                    session,
                    run_id,
                    {
                        "type": "error",
                        "code": "PLAN_COMMIT_FAILED",
                        "message": str(exc)[:500],
                    },
                )
                await mark_run_finished(
                    session,
                    run,
                    status="failed",
                    error={"code": "PLAN_COMMIT_FAILED", "message": str(exc)[:500]},
                )
                await session.commit()
        raise

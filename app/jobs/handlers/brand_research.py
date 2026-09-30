"""ai.brand_research — fetch + search + merge via SSE-over-jobs."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.features import audit
from app.features.brand_research import repository
from app.features.brand_research.tasks import run_research_pipeline
from app.integrations.llm import get_llm_router
from app.jobs.errors import TerminalError
from app.sse.bridge import emit, mark_run_finished, mark_run_running
from app.sse.models import Run

log = __import__("structlog").get_logger(__name__)


async def handle(payload: dict[str, Any]) -> None:
    try:
        run_id = uuid.UUID(str(payload["run_id"]))
        research_run_id = uuid.UUID(str(payload["research_run_id"]))
        organization_id = uuid.UUID(str(payload["organization_id"]))
        brand_id = uuid.UUID(str(payload["brand_id"]))
        user_id = uuid.UUID(str(payload["user_id"]))
        source_url = str(payload["source_url"])
        decision_kind = str(payload["decision_kind"])
    except (KeyError, ValueError, TypeError) as exc:
        raise TerminalError("BRAND_RESEARCH_BAD_PAYLOAD") from exc

    user_name = str(payload.get("user_name") or "User")
    brand_context_raw = payload.get("brand_context") or {}
    brand_context = (
        {str(k): str(v) for k, v in brand_context_raw.items()}
        if isinstance(brand_context_raw, dict)
        else {}
    )

    settings = get_settings()
    llm_router = get_llm_router(settings)
    factory = get_session_factory()

    async with factory() as session:
        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
        if run is None:
            await session.commit()
            return
        if run.status in {"succeeded", "partial", "failed"}:
            await session.commit()
            return
        await mark_run_running(session, run)
        research = await repository.get_research_run(session, research_run_id=research_run_id)
        if research is not None:
            await repository.mark_running(session, research)
        await emit(
            session,
            run_id,
            {"type": "step", "step": "brand", "status": "start"},
        )
        await session.commit()

    try:
        async with factory() as session:
            research = await repository.get_research_run(session, research_run_id=research_run_id)
            if research is None:
                raise TerminalError("BRAND_RESEARCH_MISSING_ROW")

            outcome = await run_research_pipeline(
                session,
                research=research,
                source_url=source_url,
                brand_context=brand_context,
                decision_kind=decision_kind,
                organization_id=organization_id,
                brand_id=brand_id,
                user_id=user_id,
                llm_router=llm_router,
            )

            await emit(
                session,
                run_id,
                {"type": "step", "step": "brand", "status": "done"},
            )

            if outcome["status"] == "failed":
                error = outcome["error"]
                await emit(
                    session,
                    run_id,
                    {"type": "error", "code": error["code"], "message": error["message"]},
                )
                run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
                await mark_run_finished(session, run, status="failed", error=error)
                await session.commit()
                return

            await emit(session, run_id, outcome["proposal_event"])
            decision_id = outcome.get("decision_id")
            await emit(
                session,
                run_id,
                {
                    "type": "done",
                    "decisionId": decision_id or str(research_run_id),
                },
            )
            if decision_id and not outcome.get("short_circuited"):
                await audit.record(
                    session,
                    organization_id=organization_id,
                    brand_id=brand_id,
                    actor_kind="user",
                    actor_ref=str(user_id),
                    actor_name=user_name,
                    actor_user_id=user_id,
                    action="ai.brand_researched",
                    target_type="ai_decision",
                    target_id=uuid.UUID(str(decision_id)),
                    meta={"kind": decision_kind, "sourceUrl": source_url},
                )
            run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            await mark_run_finished(
                session,
                run,
                status=outcome["status"],
                result={
                    "decisionId": decision_id,
                    "status": outcome["status"],
                },
            )
            await session.commit()
    except Exception as exc:
        log.exception("brand_research.failed", run_id=str(run_id))
        async with factory() as session:
            run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
            if run is not None:
                await emit(
                    session,
                    run_id,
                    {
                        "type": "error",
                        "code": "RESEARCH_FETCH_FAILED",
                        "message": "Failed to research brand from the website",
                    },
                )
                await mark_run_finished(
                    session,
                    run,
                    status="failed",
                    error={
                        "code": "RESEARCH_FETCH_FAILED",
                        "message": "Failed to research brand from the website",
                    },
                )
                research = await repository.get_research_run(
                    session, research_run_id=research_run_id
                )
                if research is not None and research.status == "running":
                    await repository.finalize_run(
                        session,
                        research,
                        status="failed",
                        error_message=str(exc)[:500],
                    )
                await session.commit()
        raise

"""SSE bridge queued-stall guard (JOB_NOT_STARTED)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs import queue as job_queue
from app.sse import bridge
from app.sse.bridge import create_run, stream_run_events
from app.sse.models import RunEvent

# Dedicated queue so leftover ai/running rows from other suites cannot mask the
# idle-queue predicate (bridge keys off the backing job's own queue).
_QUEUE = "sse_stall_test"


async def _collect(gen: AsyncIterator[str], *, limit: int = 20) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    async for raw in gen:
        if raw.startswith(":"):
            continue
        for line in raw.splitlines():
            if line.startswith("data: "):
                frames.append(json.loads(line[6:]))
                break
        if len(frames) >= limit:
            break
    return frames


@pytest.fixture(autouse=True)
async def _clean_stall_queue(db_session: AsyncSession) -> None:
    await db_session.execute(text("DELETE FROM jobs WHERE queue = :q"), {"q": _QUEUE})
    await db_session.commit()


@pytest.mark.asyncio
async def test_stall_emits_job_not_started_when_queue_idle(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "BRIDGE_QUEUED_STALL_SECONDS", 0.05)
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(bridge, "BRIDGE_MAX_STREAM_SECONDS", 2.0)

    org_id = uuid.uuid4()
    run = await create_run(
        db_session,
        organization_id=org_id,
        brand_id=None,
        kind="visuals_generate",
        idempotency_key=f"stall-idle:{uuid.uuid4()}",
        request_hash="abc",
        created_by=None,
    )
    await job_queue.enqueue(
        db_session,
        queue=_QUEUE,
        type="ai.visuals_generate",
        payload={"run_id": str(run.id)},
        unique_key=f"visuals_generate:{run.id}",
        organization_id=org_id,
    )
    await db_session.commit()

    frames = await _collect(stream_run_events(run.id))
    assert frames, "expected at least one SSE frame"
    assert frames[-1] == {
        "type": "error",
        "code": "JOB_NOT_STARTED",
        "message": "Job did not leave the queue in time",
    }


@pytest.mark.asyncio
async def test_stall_skips_when_queue_has_running_work(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Healthy backlog: our job is queued behind another running job — no stall."""
    monkeypatch.setattr(bridge, "BRIDGE_QUEUED_STALL_SECONDS", 0.05)
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(bridge, "BRIDGE_MAX_STREAM_SECONDS", 0.25)

    org_id = uuid.uuid4()
    run = await create_run(
        db_session,
        organization_id=org_id,
        brand_id=None,
        kind="visuals_generate",
        idempotency_key=f"stall-busy:{uuid.uuid4()}",
        request_hash="def",
        created_by=None,
    )
    await job_queue.enqueue(
        db_session,
        queue=_QUEUE,
        type="ai.visuals_generate",
        payload={"run_id": str(run.id)},
        unique_key=f"visuals_generate:{run.id}",
        organization_id=org_id,
    )
    busy_run = uuid.uuid4()
    await job_queue.enqueue(
        db_session,
        queue=_QUEUE,
        type="ai.visuals_generate",
        payload={"run_id": str(busy_run)},
        unique_key=f"visuals_generate:busy:{busy_run}",
        organization_id=org_id,
    )
    await db_session.commit()
    await db_session.execute(
        text(
            """
            UPDATE jobs SET state='running', locked_by='test-stall-busy',
                locked_at=now(), started_at=now()
            WHERE payload->>'run_id' = :busy AND queue = :q
            """
        ),
        {"busy": str(busy_run), "q": _QUEUE},
    )
    await db_session.execute(
        text(
            """
            UPDATE jobs SET state='queued', locked_by=NULL, locked_at=NULL, started_at=NULL
            WHERE payload->>'run_id' = :rid AND queue = :q
            """
        ),
        {"rid": str(run.id), "q": _QUEUE},
    )
    await db_session.commit()

    frames = await _collect(stream_run_events(run.id))
    codes = [f.get("code") for f in frames]
    assert "JOB_NOT_STARTED" not in codes
    assert frames[-1].get("code") == "STREAM_TIMEOUT"


@pytest.mark.asyncio
async def test_stall_skips_when_events_already_emitted(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "BRIDGE_QUEUED_STALL_SECONDS", 0.05)
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(bridge, "BRIDGE_MAX_STREAM_SECONDS", 0.25)

    org_id = uuid.uuid4()
    run = await create_run(
        db_session,
        organization_id=org_id,
        brand_id=None,
        kind="visuals_generate",
        idempotency_key=f"stall-events:{uuid.uuid4()}",
        request_hash="ghi",
        created_by=None,
    )
    db_session.add(
        RunEvent(
            run_id=run.id,
            seq=1,
            event={"type": "step", "step": "prompt", "status": "start"},
        )
    )
    run.status = "succeeded"
    await db_session.commit()

    frames = await _collect(stream_run_events(run.id))
    assert any(f.get("type") == "step" for f in frames)
    assert all(f.get("code") != "JOB_NOT_STARTED" for f in frames)

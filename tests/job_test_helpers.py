"""Test helpers for processing queued jobs inline.

Phase 3 moved all email sends off the request path onto the Postgres job
queue.  Integration tests that previously relied on fake.sent being populated
immediately after an HTTP call (e.g. POST /auth/forgot) now need to explicitly
*drain* the queue to exercise the handler end-to-end without running a real
worker process.

Usage:
    from tests.job_test_helpers import drain_email_jobs

    # after an HTTP call that enqueues an email.send job:
    await drain_email_jobs(db_session, fake)

The helper patches `app.jobs.handlers.email_send.get_email_provider` at module
scope for the duration of the call, so the fake receives the messages.  It
restores the real function on exit (even if an exception is raised).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.integrations.email.fakes import FakeEmailProvider


async def drain_email_jobs(
    session: AsyncSession,
    fake: FakeEmailProvider,
    *,
    type_filter: str = "email.send",
) -> int:
    """Run all pending email.send jobs inline using ``fake`` as the provider.

    Returns the number of jobs processed.
    """
    import app.jobs.handlers.email_send as _mod

    original_provider_fn = _mod.get_email_provider  # type: ignore[attr-defined]
    _mod.get_email_provider = lambda _settings: fake  # type: ignore[attr-defined, assignment]

    try:
        result = await session.execute(
            text("""
                SELECT id, payload
                FROM   jobs
                WHERE  type = :type AND state = 'queued'
                ORDER BY id
            """),
            {"type": type_filter},
        )
        rows = result.fetchall()

        from app.jobs.handlers.email_send import handle as _handle

        for row in rows:
            job_id: int = row.id
            payload: dict[str, Any] = row.payload

            try:
                await _handle(payload)
                await session.execute(
                    text(
                        "UPDATE jobs SET state='succeeded', finished_at=now() WHERE id=:id"
                    ),
                    {"id": job_id},
                )
            except Exception as exc:  # handler raised — mark failed, don't reraise
                await session.execute(
                    text(
                        "UPDATE jobs SET state='failed', last_error=:err WHERE id=:id"
                    ),
                    {"id": job_id, "err": str(exc)[:2000]},
                )

        await session.commit()
        return len(rows)

    finally:
        _mod.get_email_provider = original_provider_fn  # type: ignore[attr-defined]

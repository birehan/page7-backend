"""Postgres-native job queue (ADR-0002 / architecture/12).

Public API for enqueueing work inside a caller's own transaction:

    from app.jobs import queue
    await queue.enqueue(session, queue="sync", type="email.send", payload={"...": "..."})

The worker process (`app.workers.worker`) claims and runs handlers
registered in `app.jobs.handlers`.
"""

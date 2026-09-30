from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import get_engine


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request. Commits on a clean exit;
    an exception raised by the handler propagates past the `commit()` call
    below, and the session context manager's own exit handling rolls back
    instead. No router or service function ever calls `commit()`/`rollback()`
    itself, so a feature's write and every `audit.record()` call it makes
    inside the same request share one atomic transaction.
    """
    async with get_session_factory()() as session:
        yield session
        await session.commit()

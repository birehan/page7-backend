from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.dialects.postgresql import insert

from app.db.session import get_session_factory
from app.infrastructure.ratelimit.models import RateLimitWindow


class RateLimiter:
    """The durable, multi-replica-safe primitive every real bucket (login
    lockout, MFA-verify lockout, later the per-org AI/publish buckets) is
    built on. `slowapi` is allowed only as a local single-process fallback
    (architecture/04 §Rate limiting).

    Deliberately owns its own session/transaction per call, committing
    immediately, rather than sharing the caller's request-scoped session: a
    rate-limit increment must survive even when the request that triggered
    it goes on to fail for an unrelated reason (a wrong password, an invalid
    MFA code) and its own transaction rolls back. Sharing the caller's
    session would roll the counter back right along with it, silently
    turning every lockout into a no-op — the increment has to be an
    independent fact, not a side effect the rest of the request can undo.
    """

    @staticmethod
    def _window_start(now: datetime, window: timedelta) -> datetime:
        epoch_seconds = int(now.timestamp())
        window_seconds = int(window.total_seconds())
        floored = epoch_seconds - (epoch_seconds % window_seconds)
        return datetime.fromtimestamp(floored, tz=now.tzinfo)

    async def increment(
        self,
        *,
        bucket: str,
        now: datetime,
        window: timedelta,
        window_start: datetime | None = None,
    ) -> int:
        """Increments the counter for `bucket`'s current fixed window and returns
        the new count. `INSERT ... ON CONFLICT DO UPDATE SET count = count + 1
        RETURNING count` — a single round trip, race-free under concurrent callers.

        Pass `window_start` to pin the window to an explicit calendar boundary
        (e.g. start of the Riyadh day) instead of flooring on the Unix epoch.
        """
        start = window_start if window_start is not None else self._window_start(now, window)
        stmt = (
            insert(RateLimitWindow)
            .values(bucket=bucket, window_start=start, count=1)
            .on_conflict_do_update(
                index_elements=["bucket", "window_start"],
                set_={"count": RateLimitWindow.count + 1},
            )
            .returning(RateLimitWindow.count)
        )
        async with get_session_factory()() as session:
            result = await session.execute(stmt)
            count = result.scalar_one()
            await session.commit()
        return count

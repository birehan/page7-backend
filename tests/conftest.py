# ruff: noqa: E402
# Env must be rewritten before app.core.config / create_app import so Settings
# binds to pgblank_test, not the live acceptance DB.
from __future__ import annotations

import os
from collections.abc import AsyncIterator

# Isolate pytest from the live compose acceptance DB (`pgblank`).
# `test_migration_roundtrip` runs `alembic downgrade base` — that must never
# touch the volume-backed API database. Opt out with PYTEST_USE_LIVE_DB=1.
_raw_db = os.environ.get("DATABASE__URL", "").strip()
if (
    os.environ.get("PYTEST_USE_LIVE_DB") != "1"
    and os.environ.get("RUN_LIVE_TESTS") != "1"
):
    if not _raw_db or _raw_db.rstrip("/").endswith("/pgblank"):
        base = _raw_db.rstrip("/") if _raw_db else (
            "postgresql+asyncpg://pgblank:pgblank@localhost:5544/pgblank"
        )
        if base.endswith("/pgblank"):
            os.environ["DATABASE__URL"] = f"{base}_test"
        else:
            os.environ["DATABASE__URL"] = (
                "postgresql+asyncpg://pgblank:pgblank@localhost:5544/pgblank_test"
            )
    elif not _raw_db.rstrip("/").endswith("/pgblank_test"):
        # .env pointed somewhere else — still prefer the dedicated test DB.
        os.environ["DATABASE__URL"] = (
            "postgresql+asyncpg://pgblank:pgblank@localhost:5544/pgblank_test"
        )

# Non-live suite must not hit real providers; a local `.env` may set
# `LLM__PROVIDER=openai` / `SOCIAL__PROVIDER=zernio` for manual acceptance.
# Live tests unset this via their own skip gate and call providers directly.
if os.environ.get("RUN_LIVE_TESTS") != "1":
    os.environ["LLM__PROVIDER"] = "fake"
    os.environ["SOCIAL__PROVIDER"] = "fake"
    # Fake tests share t1/t2/t3 across many brands; Zernio's free-tier cap of 2
    # would exhaust the pool mid-suite. Live acceptance uses the real default.
    os.environ.setdefault("SOCIAL__MAX_ACCOUNTS_PER_CREDENTIAL", "10000")

# Phase 9 webhook HMAC tests need a deterministic secret for alias t1.
os.environ.setdefault("ZERNIO_WEBHOOK_SECRET__t1", "test-webhook-secret")
os.environ.setdefault("ZERNIO_API_KEY__t1", "fake-key-t1")
os.environ.setdefault("ZERNIO_API_KEY__t2", "fake-key-t2")
os.environ.setdefault("ZERNIO_API_KEY__t3", "fake-key-t3")
os.environ.setdefault("ZERNIO_API_KEY__t4", "fake-key-t4")
# Keep the pool at the aliases tests assert on unless a case opts into t4+.
os.environ.setdefault("ZERNIO_CREDENTIAL_ALIASES", "t1,t2,t3")

# Cloud Build (and any checkout without a local `.env`) has no encryption keys.
# MFA enroll/verify encrypts TOTP secrets under APP_ENCRYPTION_KEY_CURRENT.
os.environ.setdefault("APP_ENCRYPTION_KEY_CURRENT", "k1")
os.environ.setdefault("APP_ENCRYPTION_KEY__k1", "a" * 43)

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.main import create_app

get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _reset_rate_limits() -> AsyncIterator[None]:
    """Every test client goes through httpx's `ASGITransport`, which gives
    every single test the same fake peer IP (`127.0.0.1`) — so the login and
    MFA-verify lockout buckets, both keyed in part by IP
    (`app/features/auth/service.py`'s `login:ip:*`), silently accumulate
    across unrelated tests in the same process and can trip a real 429 in a
    test that never itself made 10+ attempts. Truncating between tests keeps
    each test's view of its own rate-limit buckets isolated, matching the
    isolation every other piece of per-test state already gets (fresh DB
    session, fresh app instance) — this does not touch the underlying
    production rate-limiting design, only test-process hygiene.
    """
    yield
    async with get_session_factory()() as session:
        await session.execute(text("TRUNCATE TABLE rate_limits"))
        await session.commit()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """A default `Origin` header matching `cors.allowed_origins` (the default
    dev value, `http://localhost:3000`) so `CSRFMiddleware` doesn't reject
    every mutating test by default — a test of the CSRF rejection path itself
    overrides this per-request with a disallowed or absent Origin.
    """
    app = create_app()
    transport = ASGITransport(app=app)
    allowed_origin = get_settings().cors.allowed_origins[0]
    async with AsyncClient(
        transport=transport, base_url="https://test", headers={"Origin": allowed_origin}
    ) as ac:
        yield ac

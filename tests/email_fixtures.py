"""A shared fixture for any test that needs to capture an email the app
sends (a password-reset link, an invite link) — overrides the
`get_email_provider` FastAPI dependency with a `FakeEmailProvider` rather
than hitting a real provider or parsing console log output.

Phase 3: the service layer now *enqueues* email jobs rather than sending
synchronously.  The fixture also patches the handler module's direct
`get_email_provider` call so that when tests call `drain_email()` (see
`drain_email` fixture in each directory's conftest), the fake provider
receives the messages instead of making a real network call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.integrations.email import get_email_provider
from app.integrations.email.fakes import FakeEmailProvider
from app.main import create_app


@pytest.fixture
async def client_with_fake_email() -> AsyncIterator[tuple[AsyncClient, FakeEmailProvider]]:
    import app.jobs.handlers.email_send as _email_handler

    app = create_app()
    fake = FakeEmailProvider()
    app.dependency_overrides[get_email_provider] = lambda: fake

    # Also patch the handler's direct import so `drain_email()` in tests
    # uses the same fake rather than the real email provider.
    _original_fn = _email_handler.get_email_provider  # type: ignore[attr-defined]
    _email_handler.get_email_provider = lambda _settings: fake  # type: ignore[attr-defined, assignment]

    allowed_origin = get_settings().cors.allowed_origins[0]
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport, base_url="https://test", headers={"Origin": allowed_origin}
        ) as ac:
            yield ac, fake
    finally:
        _email_handler.get_email_provider = _original_fn  # type: ignore[attr-defined]

"""Helpers for password signup + email OTP verification in integration tests.

Signup no longer issues a session cookie. Tests that need an authenticated
owner should call ``signup_and_verify`` so the verify-email job is claimed
(and marked succeeded) without depending on FakeEmailProvider.

Callers: test_signup_login_logout, test_mfa, test_invite_accept_and_team,
test_password_reset, test_session_lifecycle, test_lockout, test_enumeration_safety,
test_google_oauth, e2e journey test.

API: POST /v1/auth/signup → email_verification_required; POST /v1/auth/verify-email.
Job payload kind verify_email carries code; SessionPayload on success.

Instruction: Implement signup email verification (6-digit OTP) plan; integration tests.
"""

from __future__ import annotations

import re
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.email.fakes import FakeEmailProvider

DEFAULT_PASSWORD = "correct horse battery staple"  # noqa: S105


def extract_otp_code(fake: FakeEmailProvider) -> str:
    assert len(fake.sent) >= 1
    match = re.search(r"\b(\d{6})\b", fake.sent[-1].text)
    assert match is not None, fake.sent[-1].text
    return match.group(1)


async def claim_latest_verify_code(session: AsyncSession) -> str:
    """Read the OTP from the newest queued verify_email job and mark all such
    queued jobs done (avoids leftover verify sends polluting later drain_email).
    """
    result = await session.execute(
        text(
            """
            SELECT id, payload
            FROM   jobs
            WHERE  type = 'email.send'
              AND  state = 'queued'
              AND  payload->>'kind' = 'verify_email'
            ORDER BY id DESC
            """
        )
    )
    rows = result.fetchall()
    assert rows, "expected a queued verify_email job"
    code = rows[0].payload["code"]
    assert isinstance(code, str) and re.fullmatch(r"\d{6}", code), code
    ids = [row.id for row in rows]
    await session.execute(
        text("UPDATE jobs SET state='succeeded', finished_at=now() WHERE id = ANY(:ids)"),
        {"ids": ids},
    )
    await session.commit()
    return code


async def signup_and_verify(
    client: AsyncClient,
    db_session: AsyncSession,
    *,
    email: str,
    name: str = "Test User",
    password: str = DEFAULT_PASSWORD,
    organization_name: str = "Test Co",
    locale: str = "en",
) -> dict[str, Any]:
    """POST /auth/signup then /auth/verify-email; returns SessionPayload JSON."""
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "name": name,
            "email": email,
            "password": password,
            "organizationName": organization_name,
            "locale": locale,
        },
    )
    assert signup.status_code == 200, signup.text
    body = signup.json()
    assert body["status"] == "email_verification_required"
    assert "challengeToken" in body

    code = await claim_latest_verify_code(db_session)
    verify = await client.post(
        "/v1/auth/verify-email",
        json={"challengeToken": body["challengeToken"], "code": code},
    )
    assert verify.status_code == 200, verify.text
    return verify.json()  # type: ignore[no-any-return]

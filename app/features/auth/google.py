"""Google OIDC token exchange. Swappable in tests via monkeypatch.

Callers: app.features.auth.service (start/finish Google OAuth).
API: backs GET /auth/google/start and GET /auth/google/callback.
User instruction: Implement the plan as specified (Page7 auth + Google OIDC).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.core.config import AuthSettings
from app.core.errors import ApiError

_GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"


@dataclass(frozen=True)
class GoogleProfile:
    subject: str
    email: str
    email_verified: bool
    name: str
    picture: str | None


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
) -> str:
    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "access_type": "online",
            "prompt": "select_account",
        }
    )
    return f"{_AUTHORIZE_URL}?{params}"


async def exchange_code(code: str, *, settings: AuthSettings) -> GoogleProfile:
    """Exchange an authorization code for a Google profile (userinfo)."""
    if not settings.google_configured():
        raise ApiError(
            "GOOGLE_AUTH_UNAVAILABLE", "Google sign-in is not configured", status_code=503
        )
    assert settings.google_client_id is not None
    assert settings.google_client_secret is not None
    assert settings.google_redirect_uri is not None

    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            _GOOGLE_OAUTH_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret.get_secret_value(),
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code >= 400:
            raise ApiError("GOOGLE_AUTH_FAILED", "Google token exchange failed", status_code=401)
        tokens = token_resp.json()
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ApiError("GOOGLE_AUTH_FAILED", "Google token exchange failed", status_code=401)

        info_resp = await client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if info_resp.status_code >= 400:
            raise ApiError("GOOGLE_AUTH_FAILED", "Google userinfo failed", status_code=401)
        info = info_resp.json()

    subject = info.get("sub")
    email = info.get("email")
    if not isinstance(subject, str) or not subject:
        raise ApiError("GOOGLE_AUTH_FAILED", "Google profile missing subject", status_code=401)
    if not isinstance(email, str) or not email:
        raise ApiError("GOOGLE_AUTH_FAILED", "Google profile missing email", status_code=401)

    verified = info.get("email_verified")
    email_verified = verified is True or verified == "true"
    name_raw = info.get("name")
    name = name_raw if isinstance(name_raw, str) and name_raw.strip() else email.split("@")[0]
    picture_raw = info.get("picture")
    picture = picture_raw if isinstance(picture_raw, str) and picture_raw else None

    return GoogleProfile(
        subject=subject,
        email=email,
        email_verified=email_verified,
        name=name,
        picture=picture,
    )

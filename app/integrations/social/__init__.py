"""Social / Zernio integration factory (architecture/06 §2)."""

from __future__ import annotations

import os

from app.core.config import Settings
from app.integrations.errors import CredentialPoolExhausted
from app.integrations.social.credential_pool import (
    select_for_connect,
    select_for_new_profile,
)
from app.integrations.social.fakes import FakeSocialProvider
from app.integrations.social.ports import (
    AccountHealth,
    AccountInfo,
    AuthVerifyResult,
    ConnectUrlResult,
    PlatformOutcome,
    ProfileInfo,
    PublishRequest,
    PublishResult,
    RateLimitInfo,
    SocialProvider,
    ValidatePostResult,
)
from app.integrations.social.zernio import ZernioClient

_PLACEHOLDER_VALUES = {"", "changeme", "change-me", "placeholder", "replace_me"}
_FAKE_PROVIDER: FakeSocialProvider | None = None

__all__ = [
    "AccountHealth",
    "AccountInfo",
    "AuthVerifyResult",
    "ConnectUrlResult",
    "CredentialPoolExhausted",
    "FakeSocialProvider",
    "PlatformOutcome",
    "ProfileInfo",
    "PublishRequest",
    "PublishResult",
    "RateLimitInfo",
    "SocialProvider",
    "ValidatePostResult",
    "ZernioClient",
    "credential_aliases",
    "get_social_provider",
    "get_social_provider_for_credential",
    "reset_fake_social_provider",
    "resolve_zernio_secret",
    "select_for_connect",
    "select_for_new_profile",
    "webhook_secret_ref_for_alias",
]


def resolve_zernio_secret(secret_ref: str) -> str:
    """Look up the env var named by secret_ref (e.g. ZERNIO_API_KEY__t1).

    Matches architecture/10: zernio_credentials.secret_ref is the env var name,
    not the secret value itself.
    """
    value = os.environ.get(secret_ref)
    if not value or value.strip().lower() in _PLACEHOLDER_VALUES:
        raise RuntimeError(f"{secret_ref} is not configured")
    return value


def webhook_secret_ref_for_alias(alias: str) -> str:
    """Convention: ZERNIO_WEBHOOK_SECRET__{alias}."""
    return f"ZERNIO_WEBHOOK_SECRET__{alias}"


def credential_aliases(settings: Settings) -> list[str]:
    """Prefer ZERNIO_CREDENTIAL_ALIASES env, else settings.social.credential_aliases."""
    raw = os.environ.get("ZERNIO_CREDENTIAL_ALIASES")
    if raw is not None and raw.strip():
        return [part.strip() for part in raw.split(",") if part.strip()]
    return list(settings.social.credential_aliases)


def reset_fake_social_provider() -> FakeSocialProvider:
    """Replace the shared fake (tests)."""
    global _FAKE_PROVIDER
    _FAKE_PROVIDER = FakeSocialProvider()
    return _FAKE_PROVIDER


def _shared_fake() -> FakeSocialProvider:
    global _FAKE_PROVIDER
    if _FAKE_PROVIDER is None:
        _FAKE_PROVIDER = FakeSocialProvider()
    return _FAKE_PROVIDER


def _use_fake(settings: Settings) -> bool:
    """Resolve to FakeSocialProvider only when explicitly requested, or when
    ``auto`` and no non-placeholder ``ZERNIO_API_KEY__*`` is configured.

    Staging/production and local development behave the same for ``auto``:
    real keys → ZernioClient; missing keys → fake. Tests pin
    ``SOCIAL__PROVIDER=fake`` in conftest.
    """
    social = settings.social
    if social.provider == "fake":
        return True
    if social.provider != "auto":
        return False
    for alias in credential_aliases(settings):
        ref = f"ZERNIO_API_KEY__{alias}"
        value = os.environ.get(ref)
        if value and value.strip().lower() not in _PLACEHOLDER_VALUES:
            return False
    return True


def get_social_provider(settings: Settings, *, alias: str) -> SocialProvider:
    """Resolve SocialProvider for a credential alias."""
    if _use_fake(settings):
        return _shared_fake()
    if settings.social.provider not in ("auto", "zernio"):
        raise RuntimeError(f"unknown SOCIAL__PROVIDER={settings.social.provider!r}")
    api_key = resolve_zernio_secret(f"ZERNIO_API_KEY__{alias}")
    return ZernioClient(
        alias=alias,
        api_key=api_key,
        base_url=settings.social.base_url,
    )


def get_social_provider_for_credential(
    settings: Settings,
    *,
    alias: str,
    secret_ref: str,
) -> SocialProvider:
    """Build a provider from a DB credential row's alias + secret_ref."""
    if _use_fake(settings):
        return _shared_fake()
    api_key = resolve_zernio_secret(secret_ref)
    return ZernioClient(
        alias=alias,
        api_key=api_key,
        base_url=settings.social.base_url,
    )

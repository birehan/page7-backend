"""Read-only live Zernio smoke tests — skipped unless RUN_LIVE_TESTS=1.

Never connect or disconnect. Exercises auth/verify, accounts/health, and
validate/post dry-run against real credentials from the environment.
"""

from __future__ import annotations

import os

import pytest

from app.integrations.social.zernio import ZernioClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="Set RUN_LIVE_TESTS=1 to hit real Zernio accounts",
    ),
]

# App config uses ZERNIO_API_KEY__t1; some local .env files use ZERNIO_API_KEY_1.
_ALIAS_ENV_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("t1", ("ZERNIO_API_KEY__t1", "ZERNIO_API_KEY_1")),
    ("t2", ("ZERNIO_API_KEY__t2", "ZERNIO_API_KEY_2")),
    ("t3", ("ZERNIO_API_KEY__t3", "ZERNIO_API_KEY_3")),
)


def _configured_credentials() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for alias, env_names in _ALIAS_ENV_CANDIDATES:
        for name in env_names:
            value = os.environ.get(name, "").strip()
            if value:
                found.append((alias, value))
                break
    return found


def _require_credentials() -> list[tuple[str, str]]:
    creds = _configured_credentials()
    if not creds:
        pytest.skip(
            "No Zernio API keys configured "
            "(ZERNIO_API_KEY__t1/t2/t3 or ZERNIO_API_KEY_1/2/3)"
        )
    return creds


@pytest.mark.asyncio
async def test_auth_verify_per_credential() -> None:
    for alias, api_key in _require_credentials():
        client = ZernioClient(alias=alias, api_key=api_key)
        result = await client.verify_auth()
        assert result.valid is True, f"verify_auth failed for alias={alias}"


@pytest.mark.asyncio
async def test_accounts_health_readonly() -> None:
    alias, api_key = _require_credentials()[0]
    client = ZernioClient(alias=alias, api_key=api_key)
    health = await client.get_accounts_health()
    assert isinstance(health, list)


@pytest.mark.asyncio
async def test_validate_post_dry_run() -> None:
    alias, api_key = _require_credentials()[0]
    client = ZernioClient(alias=alias, api_key=api_key)
    result = await client.validate_post(
        {
            "content": "pgblank live dry-run — ignore",
            "platforms": [{"platform": "instagram", "accountId": "000000000000000000000000"}],
        }
    )
    assert isinstance(result.valid, bool)
    assert isinstance(result.errors, list)
    assert isinstance(result.warnings, list)

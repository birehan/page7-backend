"""Read-only live Zernio inbox — skipped unless RUN_LIVE_TESTS=1."""

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

_ALIAS_ENV_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("t1", ("ZERNIO_API_KEY__t1", "ZERNIO_API_KEY_1")),
    ("t2", ("ZERNIO_API_KEY__t2", "ZERNIO_API_KEY_2")),
    ("t3", ("ZERNIO_API_KEY__t3", "ZERNIO_API_KEY_3")),
)


def _first_credential() -> tuple[str, str]:
    for alias, env_names in _ALIAS_ENV_CANDIDATES:
        for name in env_names:
            value = os.environ.get(name, "").strip()
            if value:
                return alias, value
    pytest.skip(
        "No Zernio API keys configured "
        "(ZERNIO_API_KEY__t1/t2/t3 or ZERNIO_API_KEY_1/2/3)"
    )


@pytest.mark.asyncio
async def test_list_inbox_conversations_readonly() -> None:
    alias, api_key = _first_credential()
    client = ZernioClient(alias=alias, api_key=api_key)
    result = await client.list_inbox_conversations(limit=10)
    assert isinstance(result.conversations, list)
    assert isinstance(result.has_more, bool)

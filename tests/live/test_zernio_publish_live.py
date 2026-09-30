"""Live Zernio publish — skipped unless RUN_LIVE_TESTS=1 and RUN_LIVE_PUBLISH=1.

Creates a real post on a real connected Instagram or Facebook account. This is
intentionally double-gated: ordinary RUN_LIVE_TESTS=1 suites stay read-only.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from app.integrations.social.ports import PublishRequest
from app.integrations.social.zernio import ZernioClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="Set RUN_LIVE_TESTS=1 to hit real Zernio accounts",
    ),
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_PUBLISH") != "1",
        reason=(
            "Set RUN_LIVE_PUBLISH=1 to allow a real outbound publish "
            "(not read-only)"
        ),
    ),
]

_ALIAS_ENV_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("t1", ("ZERNIO_API_KEY__t1", "ZERNIO_API_KEY_1")),
    ("t2", ("ZERNIO_API_KEY__t2", "ZERNIO_API_KEY_2")),
    ("t3", ("ZERNIO_API_KEY__t3", "ZERNIO_API_KEY_3")),
)

_PUBLISHABLE = frozenset({"instagram", "facebook"})


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
async def test_live_publish_now_to_connected_account() -> None:
    alias, api_key = _first_credential()
    client = ZernioClient(alias=alias, api_key=api_key)

    accounts = await client.list_accounts()
    target = next(
        (
            a
            for a in accounts
            if a.platform in _PUBLISHABLE and a.is_active and not a.needs_reconnection
        ),
        None,
    )
    if target is None:
        pytest.skip(
            "No active Instagram/Facebook account on this credential — "
            "complete Phase 9 OAuth against a test account first"
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    content = (
        f"pgblank Phase 10 live publish smoke — {stamp} — safe to delete"
    )
    idem = f"pub:live-smoke:{stamp}"
    result = await client.publish(
        PublishRequest(
            content=content,
            platforms=[
                {"platform": target.platform, "accountId": target.id},
            ],
            metadata={
                "source": "pgblank_live_test",
                "stamp": stamp,
            },
            idempotency_key=idem,
        )
    )

    assert result.kind in ("created", "existing"), (
        f"unexpected kind={result.kind!r} status={result.http_status!r} "
        f"error={result.error_message!r}"
    )
    assert result.zernio_post_id, "expected zernio_post_id on successful publish"

    # Idempotent replay must not create a second post. Zernio may answer with
    # 200+existingPost (x-request-id window) or 409 content-hash pointing at
    # the same post — both are success for this smoke check.
    replay = await client.publish(
        PublishRequest(
            content=content,
            platforms=[
                {"platform": target.platform, "accountId": target.id},
            ],
            metadata={"source": "pgblank_live_test", "stamp": stamp},
            idempotency_key=idem,
        )
    )
    if replay.kind in ("created", "existing"):
        assert replay.zernio_post_id == result.zernio_post_id
    elif replay.kind == "duplicate_conflict":
        assert replay.existing_post_id == result.zernio_post_id, (
            f"409 pointed at a different post: {replay.existing_post_id!r} "
            f"vs {result.zernio_post_id!r}"
        )
    else:
        pytest.fail(
            f"replay unexpected kind={replay.kind!r} "
            f"status={replay.http_status!r} error={replay.error_message!r}"
        )

"""Zernio platform row mapping — nested accountId + processing status."""

from __future__ import annotations

import httpx

from app.integrations.social.zernio import _map_platforms, _normalize_publish_response


def test_map_platforms_unwraps_nested_account_id_and_processing() -> None:
    mapped = _map_platforms(
        [
            {
                "status": "processing",
                "platform": "instagram",
                "accountId": {"_id": "acct-1", "username": "demo"},
            }
        ]
    )
    assert len(mapped) == 1
    assert mapped[0].account_id == "acct-1"
    assert mapped[0].status == "publishing"


def test_normalize_publish_treats_processing_as_created_in_progress() -> None:
    result = _normalize_publish_response(
        201,
        {
            "post": {
                "_id": "zpost-1",
                "status": "publishing",
                "platforms": [
                    {
                        "status": "processing",
                        "platform": "instagram",
                        "accountId": {"_id": "acct-1"},
                    }
                ],
            }
        },
        httpx.Headers({}),
    )
    assert result.kind == "created"
    assert result.zernio_post_id == "zpost-1"
    assert result.platforms[0].status == "publishing"


def test_map_platforms_keeps_live_url_even_when_status_odd() -> None:
    # Callers: pytest (this file). User: "show error while it was actually posted".
    mapped = _map_platforms(
        [
            {
                "status": "failed",
                "platform": "instagram",
                "accountId": "acct-1",
                "platformPostId": "ig-123",
                "platformPostUrl": "https://www.instagram.com/p/C1a2B3c4D5e/",
            }
        ]
    )
    assert mapped[0].platform_post_url is not None
    assert mapped[0].platform_post_id == "ig-123"

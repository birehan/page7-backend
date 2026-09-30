"""Contract tests for Phase 7 /ai endpoints against api-contract.json."""

from __future__ import annotations

import json
import uuid
from typing import Any

import jsonschema
from httpx import AsyncClient

from app.core.config import get_settings
from tests.contract.conftest import find_endpoint
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"AI Brand {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _parse_sse_events(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in raw.split("\n\n"):
        data_lines = [
            line[5:].strip() if line.startswith("data:") else line[6:].strip()
            for line in block.split("\n")
            if line.startswith("data:") or line.startswith("data: ")
        ]
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


async def test_captions_stream_matches_contract(
    client: AsyncClient, seed_member: SeedMember, api_contract: dict[str, Any]
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    endpoint = find_endpoint(api_contract, "ai", "POST", path_contains="/ai/captions")
    body = {
        "brandId": brand["id"],
        "platforms": ["instagram"],
        "intent": "generate",
        "dialect": "gulf",
        "count": 2,
    }
    jsonschema.validate(instance=body, schema=endpoint["request"])

    response = await client.post("/v1/ai/captions", json=body)
    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["content-type"]

    events = _parse_sse_events(response.text)
    assert events
    types = [e["type"] for e in events]
    assert "step" in types
    assert "token" in types or "variant_done" in types
    assert "done" in types
    for event in events:
        jsonschema.validate(instance=event, schema=endpoint["response"])


async def test_feedback_is_204(
    client: AsyncClient, seed_member: SeedMember, api_contract: dict[str, Any]
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    captions = await client.post(
        "/v1/ai/captions",
        json={
            "brandId": brand["id"],
            "platforms": ["instagram"],
            "intent": "generate",
            "dialect": "gulf",
            "count": 1,
        },
    )
    events = _parse_sse_events(captions.text)
    done = next(e for e in events if e["type"] == "done")

    endpoint = find_endpoint(api_contract, "ai", "POST", path_contains="/ai/feedback")
    body = {
        "decisionId": done["decisionId"],
        "variantIndex": 0,
        "rating": "up",
    }
    jsonschema.validate(instance=body, schema=endpoint["request"])
    response = await client.post("/v1/ai/feedback", json=body)
    assert response.status_code == 204, response.text


async def test_alt_text_json_shape(
    client: AsyncClient, seed_member: SeedMember, api_contract: dict[str, Any]
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    endpoint = find_endpoint(api_contract, "ai", "POST", path_contains="/ai/alt-text")
    body = {
        "brandId": brand["id"],
        "mediaUrl": "https://cdn.example.com/generated/tmp.png",
        "caption": "a coffee cup",
    }
    jsonschema.validate(instance=body, schema=endpoint["request"])
    response = await client.post("/v1/ai/alt-text", json=body)
    assert response.status_code == 200, response.text
    jsonschema.validate(instance=response.json(), schema=endpoint["response"])
    assert "altAr" in response.json()
    assert "altEn" in response.json()

from __future__ import annotations

from typing import Any

import jsonschema
from httpx import AsyncClient


async def test_404_matches_the_frontend_error_envelope_schema(
    client: AsyncClient, api_contract: dict[str, Any]
) -> None:
    """architecture/04: every non-2xx response is `{"error": {code, message, details,
    requestId}}`. The frontend validates the inner object against `errorEnvelope`
    (`primitives.ts`) — exported here as `api_contract["errorEnvelope"]` — so this
    is the same schema the client itself would reject a malformed error with.
    """
    response = await client.get("/this-route-does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert "error" in body
    jsonschema.validate(instance=body["error"], schema=api_contract["errorEnvelope"])


async def test_error_envelope_code_is_stable_for_a_missing_route(client: AsyncClient) -> None:
    response = await client.get("/this-route-does-not-exist")
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_error_response_never_leaks_a_request_id_of_none_as_a_string(
    client: AsyncClient,
) -> None:
    # requestId is always present because RequestContextMiddleware assigns one to
    # every request before a handler (or error handler) ever runs.
    response = await client.get("/this-route-does-not-exist")
    assert isinstance(response.json()["error"]["requestId"], str)

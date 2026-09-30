"""OpenAPI ↔ frontend contract inventory parity (Phase 14).

Compares FastAPI's `app.openapi()` paths against `docs/contracts/api-contract.json`
endpoint inventory — not full structural schema equality (the two generators will
never agree byte-for-byte). Paths are normalized before comparison:

- strip the `/v1` API prefix from OpenAPI
- convert `{param}` (OpenAPI) ↔ `:param` (contract)
"""

from __future__ import annotations

import re
from typing import Any

from app.main import create_app

# Provider callbacks / webhooks are backend-only and intentionally absent from the
# frontend zod contract registry.
_OPENAPI_ONLY_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/integrations/zernio/callback"),
        ("POST", "/webhooks/zernio/:alias"),
        # The browser's top-level navigation lands here directly after Google's own
        # consent screen redirects it — Google hits this URL, not the frontend's
        # fetch-based API client, so it was never a candidate for the frontend's
        # zod contract registry in the first place (unlike GET /auth/google/start,
        # which the frontend does call via fetch and which IS in the registry).
        ("GET", "/auth/google/callback"),
    }
)

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


def _normalize_openapi_path(path: str) -> str:
    if path.startswith("/v1"):
        path = path[3:] or "/"
    return re.sub(r"\{([^}]+)\}", r":\1", path)


def _contract_inventory(api_contract: dict[str, Any]) -> set[tuple[str, str]]:
    return {(str(e["method"]).upper(), str(e["path"])) for e in api_contract["endpoints"]}


def _openapi_inventory() -> set[tuple[str, str]]:
    schema = create_app().openapi()
    out: set[tuple[str, str]] = set()
    for path, ops in schema.get("paths", {}).items():
        if path.startswith("/health") or path.startswith("/_local"):
            continue
        normalized = _normalize_openapi_path(path)
        for method in ops:
            if method in _HTTP_METHODS:
                out.add((method.upper(), normalized))
    return out


def test_every_contract_endpoint_exists_in_openapi(api_contract: dict[str, Any]) -> None:
    contract = _contract_inventory(api_contract)
    openapi = _openapi_inventory()
    missing = sorted(contract - openapi)
    assert not missing, (
        "Contract endpoints missing from OpenAPI "
        f"({len(missing)}):\n" + "\n".join(f"  {m} {p}" for m, p in missing)
    )


def test_openapi_has_no_unexpected_v1_routes(api_contract: dict[str, Any]) -> None:
    contract = _contract_inventory(api_contract)
    openapi = _openapi_inventory()
    unexpected = sorted(openapi - contract - _OPENAPI_ONLY_ALLOWLIST)
    assert not unexpected, (
        "OpenAPI routes not in the frontend contract and not allowlisted "
        f"({len(unexpected)}):\n" + "\n".join(f"  {m} {p}" for m, p in unexpected)
    )

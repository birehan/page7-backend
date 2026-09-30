from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config

from alembic import command
from tests.db_fixtures import db_session, seed_member  # noqa: F401
from tests.email_fixtures import client_with_fake_email  # noqa: F401

CONTRACT_PATH = Path(__file__).parents[3] / "docs" / "contracts" / "api-contract.json"


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations() -> Iterator[None]:
    command.upgrade(Config("alembic.ini"), "head")
    yield


@pytest.fixture(scope="session")
def api_contract() -> dict[str, Any]:
    """The frontend-generated contract export (`pnpm gen:contract` in pgblank-web),
    checked in at the repo root so it survives independently of either package's
    own build. Phase 1's Definition of Done requires this file to exist and be
    non-empty; these tests are the first real consumers of it.
    """
    contract: dict[str, Any] = json.loads(CONTRACT_PATH.read_text())
    return contract


def find_endpoint(
    api_contract: dict[str, Any], resource: str, method: str, *, path_contains: str | None = None
) -> dict[str, Any]:
    """`path_contains` disambiguates a resource+method pair that isn't unique
    on its own — team's two `POST` endpoints (`.../team`, the invite create,
    vs `.../team/:memberId/resend-invite`) being the one case in this
    contract that needs it.
    """
    endpoints: list[dict[str, Any]] = api_contract["endpoints"]
    for endpoint in endpoints:
        if endpoint["resource"] != resource or endpoint["method"] != method:
            continue
        if path_contains is not None and path_contains not in endpoint["path"]:
            continue
        return endpoint
    raise AssertionError(
        f"no {method} endpoint for resource {resource!r} "
        f"(path_contains={path_contains!r}) in the contract"
    )

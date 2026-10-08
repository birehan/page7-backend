from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.core.config import Environment, Settings
from app.core.errors import ApiError
from app.features.media.local_storage_router import _require_local
from app.integrations.storage.local import LocalStorage


def _storage(root: Path) -> LocalStorage:
    return LocalStorage(root=root, public_base_url="http://localhost:8000", public_bucket="pub")


def _settings(env: Environment, provider: str) -> Settings:
    return cast(Settings, SimpleNamespace(app_env=env, storage=SimpleNamespace(provider=provider)))


@pytest.mark.parametrize("env", [Environment.PRODUCTION, Environment.STAGING])
@pytest.mark.parametrize("provider", ["local", "auto", "r2"])
def test_unauthenticated_local_storage_is_closed_outside_dev(
    env: Environment, provider: str, tmp_path: Path
) -> None:
    # STORAGE__PROVIDER=local must not open the unauthenticated PUT/GET surface.
    with pytest.raises(ApiError) as exc:
        _require_local(_settings(env, provider), _storage(tmp_path))
    assert exc.value.status_code == 404


@pytest.mark.parametrize("env", [Environment.DEVELOPMENT, Environment.TESTING])
def test_local_storage_is_available_in_dev_and_test(env: Environment, tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    assert _require_local(_settings(env, "local"), storage) is storage


def test_key_cannot_escape_into_a_sibling_directory(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    resolve: Any = storage._resolve
    # "pub-evil" shares the "pub" prefix, which a plain startswith check would allow.
    with pytest.raises(ValueError):
        resolve("public", "../pub-evil/secret.txt")
    with pytest.raises(ValueError):
        resolve("public", "../../etc/passwd")


def test_normal_nested_key_resolves_inside_the_bucket(tmp_path: Path) -> None:
    resolved = _storage(tmp_path)._resolve("public", "brands/abc/logo.png")
    assert resolved.is_relative_to((tmp_path / "pub").resolve())

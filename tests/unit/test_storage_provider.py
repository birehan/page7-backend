from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Environment, Settings, StorageSettings
from app.integrations.storage import get_object_storage
from app.integrations.storage.fakes import FakeObjectStorage
from app.integrations.storage.local import LocalStorage
from app.integrations.storage.ports import CopyObjectRequest, PresignUploadRequest
from app.integrations.storage.r2 import R2Storage


async def test_fake_object_storage_round_trip() -> None:
    storage = FakeObjectStorage()
    await storage.put_object("public", "a/b", b"hello", content_type="text/plain")
    head = await storage.head_object("public", "a/b")
    assert head.exists is True
    assert head.size_bytes == 5
    assert head.content_type == "text/plain"
    assert await storage.get_object("public", "a/b") == b"hello"
    assert await storage.get_object_range("public", "a/b", start=1, end=3) == b"ell"
    await storage.delete_object("public", "a/b")
    assert (await storage.head_object("public", "a/b")).exists is False


async def test_fake_presign_returns_content_type_header() -> None:
    storage = FakeObjectStorage()
    result = await storage.presign_upload(
        PresignUploadRequest(bucket="public", key="tmp/1/original", content_type="image/png")
    )
    assert result.required_headers == {"Content-Type": "image/png"}
    assert "tmp/1/original" in result.upload_url


async def test_local_storage_copy_internal(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path, public_base_url="http://localhost/media")
    await storage.put_object("public", "tmp/1/original", b"png-bytes", content_type="image/png")
    await storage.copy_object(
        CopyObjectRequest(
            source="r2://pgblank/tmp/1/original",
            dest_bucket="public",
            dest_key="orgs/o/brands/b/media/x/original",
        )
    )
    assert (
        await storage.get_object("public", "orgs/o/brands/b/media/x/original") == b"png-bytes"
    )
    assert storage.public_url("orgs/o/brands/b/media/x/original").endswith(
        "/orgs/o/brands/b/media/x/original"
    )


def test_factory_resolves_to_local_under_development(tmp_path: Path) -> None:
    settings = Settings(
        app_env=Environment.DEVELOPMENT,
        storage=StorageSettings(provider="auto", local_root=str(tmp_path)),
        _env_file=None,
    )
    assert isinstance(get_object_storage(settings), LocalStorage)


def test_empty_public_base_url_falls_back_to_localhost() -> None:
    """Blank STORAGE__PUBLIC_BASE_URL must not produce relative upload URLs."""
    storage = StorageSettings(public_base_url="")
    assert storage.public_base_url == "http://localhost:8000"
    whitespace = StorageSettings(public_base_url="   ")
    assert whitespace.public_base_url == "http://localhost:8000"


def test_factory_resolves_to_r2_when_configured() -> None:
    settings = Settings(
        app_env=Environment.PRODUCTION,
        app_encryption_key_current="k1",
        app_encryption_key={"k1": "a" * 43},
        storage=StorageSettings(
            provider="r2",
            r2_account_id="acct",
            r2_access_key_id="key",
            r2_secret_access_key="secret",  # noqa: S106
            public_base_url="https://media.example.com",
        ),
        _env_file=None,
    )
    assert isinstance(get_object_storage(settings), R2Storage)


def test_factory_raises_without_r2_credentials_outside_local_envs() -> None:
    settings = Settings(
        app_env=Environment.PRODUCTION,
        app_encryption_key_current="k1",
        app_encryption_key={"k1": "a" * 43},
        storage=StorageSettings(provider="r2"),
        _env_file=None,
    )
    with pytest.raises(RuntimeError, match="STORAGE__R2"):
        get_object_storage(settings)


async def test_private_bucket_unset_raises(tmp_path: Path) -> None:
    local = LocalStorage(
        root=tmp_path,
        public_base_url="http://localhost/media",
        private_bucket=None,
    )
    with pytest.raises(RuntimeError, match="PRIVATE_BUCKET"):
        await local.head_object("private", "invoices/1.pdf")

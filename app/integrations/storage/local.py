from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from app.integrations.errors import ProviderUnavailableError
from app.integrations.storage.ports import (
    Bucket,
    CopyObjectRequest,
    HeadObjectResult,
    PresignUploadRequest,
    PresignUploadResult,
)


class LocalStorage:
    """Filesystem-backed ObjectStorage for Compose and integration tests
    (architecture/11 §1). Same key layout as R2; zero network I/O for our own
    objects. External `copy_object` sources go through SSRF-safe fetch.
    """

    def __init__(
        self,
        *,
        root: Path,
        public_base_url: str,
        public_bucket: str = "pgblank",
        private_bucket: str | None = None,
    ) -> None:
        self._root = root
        self._public_base_url = public_base_url.rstrip("/")
        self._bucket_names: dict[Bucket, str | None] = {
            "public": public_bucket,
            "private": private_bucket,
        }
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, bucket: Bucket, key: str) -> Path:
        name = self._bucket_names[bucket]
        if name is None:
            raise RuntimeError(
                f"STORAGE__PRIVATE_BUCKET is not configured; cannot use bucket={bucket!r}"
            )
        bucket_root = (self._root / name).resolve()
        path = (bucket_root / key).resolve()
        # `startswith` would accept a sibling like "<bucket>-other"; require real containment.
        if not path.is_relative_to(bucket_root):
            raise ValueError("storage key escapes bucket root")
        return path

    async def presign_upload(self, request: PresignUploadRequest) -> PresignUploadResult:
        # LocalStorage never issues a real signed URL — the API's own
        # `/_local-storage/{bucket}/{key}` PUT handler receives the bytes.
        return PresignUploadResult(
            upload_url=(
                f"{self._public_base_url}/_local-storage/"
                f"{request.bucket}/{quote(request.key, safe='/')}"
            ),
            required_headers={"Content-Type": request.content_type},
        )

    async def presign_download(
        self,
        bucket: Literal["private"],
        key: str,
        expires_in: timedelta = timedelta(minutes=15),
    ) -> str:
        _ = expires_in
        return (
            f"{self._public_base_url}/_local-storage/"
            f"{bucket}/{quote(key, safe='/')}"
        )

    async def head_object(self, bucket: Bucket, key: str) -> HeadObjectResult:
        path = self._resolve(bucket, key)
        if not path.is_file():
            return HeadObjectResult(exists=False)
        content_type = "application/octet-stream"
        meta = path.with_suffix(path.suffix + ".content-type")
        if meta.is_file():
            content_type = meta.read_text(encoding="utf-8").strip() or content_type
        return HeadObjectResult(
            exists=True,
            size_bytes=path.stat().st_size,
            content_type=content_type,
            etag=f'"{path.stat().st_size:x}"',
        )

    async def delete_object(self, bucket: Bucket, key: str) -> None:
        path = self._resolve(bucket, key)
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".content-type").unlink(missing_ok=True)

    async def copy_object(self, request: CopyObjectRequest) -> None:
        dest = self._resolve(request.dest_bucket, request.dest_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if request.source.startswith("r2://"):
            _, rest = request.source.split("r2://", 1)
            src_bucket_name, src_key = rest.split("/", 1)
            src_bucket = self._logical_bucket(src_bucket_name)
            src = self._resolve(src_bucket, src_key)
            if not src.is_file():
                raise FileNotFoundError(request.source)
            shutil.copy2(src, dest)
            content_type = request.content_type
            if content_type is None:
                meta = src.with_suffix(src.suffix + ".content-type")
                content_type = (
                    meta.read_text(encoding="utf-8").strip()
                    if meta.is_file()
                    else "application/octet-stream"
                )
            dest.with_suffix(dest.suffix + ".content-type").write_text(
                content_type, encoding="utf-8"
            )
            return

        if not request.source.startswith(("http://", "https://")):
            raise ValueError(f"unsupported copy source: {request.source}")
        from app.integrations.webfetch.ssrf import safe_connect

        try:
            response = await safe_connect(
                request.source,
                max_bytes=20 * 1024 * 1024,
                timeout_seconds=60.0,
            )
            if response.status_code >= 400:
                raise ProviderUnavailableError(
                    f"external copy source returned {response.status_code}"
                )
            body = response.content
            content_type = (
                request.content_type
                or response.content_type
                or "application/octet-stream"
            )
        except ProviderUnavailableError:
            raise
        except Exception as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        dest.write_bytes(body)
        dest.with_suffix(dest.suffix + ".content-type").write_text(
            content_type, encoding="utf-8"
        )

    def _logical_bucket(self, name: str) -> Bucket:
        if name in {"public", self._bucket_names["public"]}:
            return "public"
        if name in {"private", self._bucket_names["private"]}:
            return "private"
        raise ValueError(f"unknown storage bucket name: {name}")

    async def get_object_range(
        self, bucket: Bucket, key: str, *, start: int, end: int
    ) -> bytes:
        path = self._resolve(bucket, key)
        with path.open("rb") as handle:
            handle.seek(start)
            return handle.read(end - start + 1)

    async def get_object(self, bucket: Bucket, key: str) -> bytes:
        return self._resolve(bucket, key).read_bytes()

    async def put_object(
        self,
        bucket: Bucket,
        key: str,
        body: bytes,
        *,
        content_type: str,
    ) -> None:
        path = self._resolve(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.with_suffix(path.suffix + ".content-type").write_text(
            content_type, encoding="utf-8"
        )

    def public_url(self, key: str) -> str:
        return (
            f"{self._public_base_url}/_local-storage/public/{quote(key, safe='/')}"
        )

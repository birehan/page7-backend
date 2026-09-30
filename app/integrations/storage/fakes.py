from __future__ import annotations

from datetime import timedelta
from typing import Literal
from urllib.parse import quote

from app.integrations.storage.ports import (
    Bucket,
    CopyObjectRequest,
    HeadObjectResult,
    PresignUploadRequest,
    PresignUploadResult,
)


class FakeObjectStorage:
    """In-memory ObjectStorage for pure unit tests (architecture/06 §5)."""

    def __init__(self, *, public_base_url: str = "https://media.test") -> None:
        self.public_base_url = public_base_url.rstrip("/")
        self.objects: dict[tuple[Bucket, str], tuple[bytes, str]] = {}
        self.deleted: list[tuple[Bucket, str]] = []
        self.presigned: list[PresignUploadRequest] = []

    def _bucket_name(self, bucket: Bucket) -> str:
        return bucket

    async def presign_upload(self, request: PresignUploadRequest) -> PresignUploadResult:
        self.presigned.append(request)
        return PresignUploadResult(
            upload_url=(
                f"{self.public_base_url}/presign/{request.bucket}/"
                f"{quote(request.key, safe='/')}"
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
        return f"{self.public_base_url}/download/{bucket}/{quote(key, safe='/')}"

    async def head_object(self, bucket: Bucket, key: str) -> HeadObjectResult:
        item = self.objects.get((bucket, key))
        if item is None:
            return HeadObjectResult(exists=False)
        body, content_type = item
        return HeadObjectResult(
            exists=True,
            size_bytes=len(body),
            content_type=content_type,
            etag=f'"{len(body):x}"',
        )

    async def delete_object(self, bucket: Bucket, key: str) -> None:
        self.objects.pop((bucket, key), None)
        self.deleted.append((bucket, key))

    async def copy_object(self, request: CopyObjectRequest) -> None:
        if request.source.startswith("r2://"):
            _, rest = request.source.split("r2://", 1)
            src_bucket_name, src_key = rest.split("/", 1)
            src_bucket: Bucket = (
                "private" if src_bucket_name in {"private", "pgblank-private"} else "public"
            )
            item = self.objects.get((src_bucket, src_key))
            if item is None:
                raise FileNotFoundError(request.source)
            body, content_type = item
        elif request.source.startswith(("http://", "https://")):
            # Unit tests inject bytes via put_object; external URLs are stubbed
            # as empty so callers that need real bytes use put_object first.
            body = b""
            content_type = request.content_type or "application/octet-stream"
        else:
            raise ValueError(f"unsupported copy source: {request.source}")
        if request.content_type is not None:
            content_type = request.content_type
        self.objects[(request.dest_bucket, request.dest_key)] = (body, content_type)

    async def get_object_range(
        self, bucket: Bucket, key: str, *, start: int, end: int
    ) -> bytes:
        body, _ = self.objects[(bucket, key)]
        return body[start : end + 1]

    async def get_object(self, bucket: Bucket, key: str) -> bytes:
        body, _ = self.objects[(bucket, key)]
        return body

    async def put_object(
        self,
        bucket: Bucket,
        key: str,
        body: bytes,
        *,
        content_type: str,
    ) -> None:
        self.objects[(bucket, key)] = (body, content_type)

    def public_url(self, key: str) -> str:
        return f"{self.public_base_url}/{quote(key, safe='/')}"

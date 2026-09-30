from __future__ import annotations

from datetime import timedelta
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

Bucket = Literal["public", "private"]


class PresignUploadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bucket: Bucket
    key: str
    content_type: str
    expires_in: timedelta = timedelta(hours=1)


class PresignUploadResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    upload_url: str
    required_headers: dict[str, str]


class HeadObjectResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exists: bool
    size_bytes: int | None = None
    content_type: str | None = None
    etag: str | None = None


class CopyObjectRequest(BaseModel):
    """`source` is either an external https URL or `r2://{bucket}/{key}` for an
    object already in our own storage (architecture/11 §1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    dest_bucket: Bucket
    dest_key: str
    content_type: str | None = None


@runtime_checkable
class ObjectStorage(Protocol):
    async def presign_upload(self, request: PresignUploadRequest) -> PresignUploadResult: ...

    async def presign_download(
        self,
        bucket: Literal["private"],
        key: str,
        expires_in: timedelta = timedelta(minutes=15),
    ) -> str: ...

    async def head_object(self, bucket: Bucket, key: str) -> HeadObjectResult: ...

    async def delete_object(self, bucket: Bucket, key: str) -> None: ...

    async def copy_object(self, request: CopyObjectRequest) -> None: ...

    async def get_object_range(
        self, bucket: Bucket, key: str, *, start: int, end: int
    ) -> bytes: ...

    async def get_object(self, bucket: Bucket, key: str) -> bytes: ...

    async def put_object(
        self,
        bucket: Bucket,
        key: str,
        body: bytes,
        *,
        content_type: str,
    ) -> None: ...

    def public_url(self, key: str) -> str: ...

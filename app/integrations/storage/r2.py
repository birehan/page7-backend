from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal
from urllib.parse import quote

import aioboto3  # type: ignore[import-untyped]
from botocore.config import Config
from botocore.exceptions import ClientError

from app.integrations.errors import ProviderUnavailableError
from app.integrations.storage.ports import (
    Bucket,
    CopyObjectRequest,
    HeadObjectResult,
    PresignUploadRequest,
    PresignUploadResult,
)

# architecture/11 §4: disable automatic checksum-header injection so the
# signed header set is exactly what `required_headers` tells the client to
# send (today: just Content-Type). Without this, every browser PUT fails
# with an opaque 403 SignatureDoesNotMatch against R2.
_S3_CONFIG = Config(
    signature_version="s3v4",
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required",
)


class R2Storage:
    """The only file allowed to `import aioboto3` — pyproject.toml's
    import-linter contract names this module as its one exception.
    """

    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        public_bucket: str,
        public_base_url: str,
        private_bucket: str | None = None,
    ) -> None:
        self._endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._public_base_url = public_base_url.rstrip("/")
        self._bucket_names: dict[Bucket, str | None] = {
            "public": public_bucket,
            "private": private_bucket,
        }
        self._session = aioboto3.Session()

    def _bucket(self, bucket: Bucket) -> str:
        name = self._bucket_names[bucket]
        if name is None:
            raise RuntimeError(
                f"STORAGE__PRIVATE_BUCKET is not configured; cannot use bucket={bucket!r}"
            )
        return name

    def _client_kwargs(self) -> dict[str, Any]:
        return {
            "service_name": "s3",
            "endpoint_url": self._endpoint,
            "aws_access_key_id": self._access_key_id,
            "aws_secret_access_key": self._secret_access_key,
            "region_name": "auto",
            "config": _S3_CONFIG,
        }

    async def presign_upload(self, request: PresignUploadRequest) -> PresignUploadResult:
        try:
            async with self._session.client(**self._client_kwargs()) as client:
                url = await client.generate_presigned_url(
                    ClientMethod="put_object",
                    Params={
                        "Bucket": self._bucket(request.bucket),
                        "Key": request.key,
                        "ContentType": request.content_type,
                    },
                    ExpiresIn=int(request.expires_in.total_seconds()),
                )
        except ClientError as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        return PresignUploadResult(
            upload_url=url,
            required_headers={"Content-Type": request.content_type},
        )

    async def presign_download(
        self,
        bucket: Literal["private"],
        key: str,
        expires_in: timedelta = timedelta(minutes=15),
    ) -> str:
        try:
            async with self._session.client(**self._client_kwargs()) as client:
                url: str = await client.generate_presigned_url(
                    ClientMethod="get_object",
                    Params={"Bucket": self._bucket(bucket), "Key": key},
                    ExpiresIn=int(expires_in.total_seconds()),
                )
                return url
        except ClientError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

    async def head_object(self, bucket: Bucket, key: str) -> HeadObjectResult:
        try:
            async with self._session.client(**self._client_kwargs()) as client:
                response = await client.head_object(
                    Bucket=self._bucket(bucket), Key=key
                )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return HeadObjectResult(exists=False)
            raise ProviderUnavailableError(str(exc)) from exc
        return HeadObjectResult(
            exists=True,
            size_bytes=int(response["ContentLength"]),
            content_type=response.get("ContentType"),
            etag=response.get("ETag"),
        )

    async def delete_object(self, bucket: Bucket, key: str) -> None:
        try:
            async with self._session.client(**self._client_kwargs()) as client:
                await client.delete_object(Bucket=self._bucket(bucket), Key=key)
        except ClientError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

    async def copy_object(self, request: CopyObjectRequest) -> None:
        if request.source.startswith("r2://"):
            await self._copy_internal(request)
            return
        if not request.source.startswith(("http://", "https://")):
            raise ValueError(f"unsupported copy source: {request.source}")
        await self._copy_external(request)

    async def _copy_internal(self, request: CopyObjectRequest) -> None:
        _, rest = request.source.split("r2://", 1)
        src_bucket_name, src_key = rest.split("/", 1)
        copy_source = {"Bucket": src_bucket_name, "Key": src_key}
        extra: dict[str, Any] = {}
        if request.content_type is not None:
            extra["ContentType"] = request.content_type
            extra["MetadataDirective"] = "REPLACE"
        try:
            async with self._session.client(**self._client_kwargs()) as client:
                await client.copy_object(
                    Bucket=self._bucket(request.dest_bucket),
                    Key=request.dest_key,
                    CopySource=copy_source,
                    **extra,
                )
        except ClientError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

    async def _copy_external(self, request: CopyObjectRequest) -> None:
        # External https source → SSRF-safe fetch → PUT (architecture/09 §4,
        # architecture/05 §7). Fal URLs and stock-photo imports both go here.
        from app.integrations.webfetch.ssrf import safe_connect

        try:
            response = await safe_connect(
                request.source,
                max_bytes=20 * 1024 * 1024,
                timeout_seconds=120.0,
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
        await self.put_object(
            request.dest_bucket,
            request.dest_key,
            body,
            content_type=content_type,
        )

    async def get_object_range(
        self, bucket: Bucket, key: str, *, start: int, end: int
    ) -> bytes:
        try:
            async with self._session.client(**self._client_kwargs()) as client:
                response = await client.get_object(
                    Bucket=self._bucket(bucket),
                    Key=key,
                    Range=f"bytes={start}-{end}",
                )
                raw = await response["Body"].read()
        except ClientError as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        return bytes(raw)

    async def get_object(self, bucket: Bucket, key: str) -> bytes:
        try:
            async with self._session.client(**self._client_kwargs()) as client:
                response = await client.get_object(
                    Bucket=self._bucket(bucket), Key=key
                )
                raw = await response["Body"].read()
        except ClientError as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        return bytes(raw)

    async def put_object(
        self,
        bucket: Bucket,
        key: str,
        body: bytes,
        *,
        content_type: str,
    ) -> None:
        try:
            async with self._session.client(**self._client_kwargs()) as client:
                await client.put_object(
                    Bucket=self._bucket(bucket),
                    Key=key,
                    Body=body,
                    ContentType=content_type,
                )
        except ClientError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

    def public_url(self, key: str) -> str:
        return f"{self._public_base_url}/{quote(key, safe='/')}"

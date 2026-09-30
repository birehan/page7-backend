"""Dev/test-only LocalStorage HTTP surface.

LocalStorage's presigned URL points here so the real frontend PUT flow works
against Compose without R2. Never mounted when STORAGE__PROVIDER resolves to R2.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import Response as FastAPIResponse

from app.core.config import Environment, Settings, get_settings
from app.core.errors import ApiError
from app.integrations.storage import get_object_storage
from app.integrations.storage.local import LocalStorage
from app.integrations.storage.ports import Bucket, ObjectStorage

router = APIRouter(tags=["local-storage"])


def _require_local(
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> LocalStorage:
    if settings.app_env not in (Environment.DEVELOPMENT, Environment.TESTING) and (
        settings.storage.provider != "local"
    ):
        raise ApiError("NOT_FOUND", "Not found", status_code=404)
    if not isinstance(storage, LocalStorage):
        raise ApiError("NOT_FOUND", "Not found", status_code=404)
    return storage


@router.put("/_local-storage/{bucket}/{key:path}")
async def put_local_object(
    bucket: Bucket,
    key: str,
    request: Request,
    storage: Annotated[LocalStorage, Depends(_require_local)],
) -> Response:
    body = await request.body()
    content_type = request.headers.get("content-type") or "application/octet-stream"
    await storage.put_object(
        bucket, unquote(key), body, content_type=content_type
    )
    return Response(status_code=200)


@router.get("/_local-storage/{bucket}/{key:path}")
async def get_local_object(
    bucket: Bucket,
    key: str,
    storage: Annotated[LocalStorage, Depends(_require_local)],
) -> FastAPIResponse:
    head = await storage.head_object(bucket, unquote(key))
    if not head.exists:
        raise ApiError("NOT_FOUND", "Not found", status_code=404)
    body = await storage.get_object(bucket, unquote(key))
    return FastAPIResponse(
        content=body,
        media_type=head.content_type or "application/octet-stream",
    )

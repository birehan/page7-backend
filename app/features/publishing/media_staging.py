"""Make publish media URLs fetchable by Zernio.

Zernio rejects localhost / private URLs (SSRF guard) and requires HTTPS. Local
development stores media in LocalStorage at ``http://localhost:8000/_local-storage/...``,
which Facebook/Zernio cannot reach. When the configured SocialProvider is Zernio,
we stage those bytes through Zernio's ``POST /v1/media/presign`` and swap in the
returned ``publicUrl`` before ``publish``.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

import structlog

from app.integrations.social.ports import PublishRequest, SocialProvider
from app.integrations.social.zernio import ZernioClient
from app.integrations.storage.ports import ObjectStorage

logger = structlog.get_logger(__name__)


def is_provider_fetchable_url(url: str) -> bool:
    """True when a remote publisher (Zernio) can safely GET the media URL."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


def storage_key_from_media_url(url: str, *, public_base_url: str) -> str | None:
    """Map a LocalStorage or R2 public URL back to the object key."""
    base = public_base_url.rstrip("/")
    if base and url.startswith(base + "/"):
        rest = url[len(base) + 1 :]
        marker = "_local-storage/public/"
        if rest.startswith(marker):
            return rest[len(marker) :]
        return rest
    marker = "/_local-storage/public/"
    if marker in url:
        return url.split(marker, 1)[1].lstrip("/")
    parsed = urlparse(url)
    if parsed.path.startswith("/"):
        if marker in parsed.path:
            return parsed.path.split(marker, 1)[1].lstrip("/")
        path = parsed.path.lstrip("/")
        return path or None
    return None


async def ensure_media_urls_provider_fetchable(
    request: PublishRequest,
    *,
    storage: ObjectStorage,
    provider: SocialProvider,
    public_base_url: str,
) -> PublishRequest:
    """Rewrite non-public media URLs via Zernio staging when needed."""
    if not request.media_items:
        return request
    if all(is_provider_fetchable_url(str(item.get("url", ""))) for item in request.media_items):
        return request
    if not isinstance(provider, ZernioClient):
        # FakeSocialProvider accepts LocalStorage URLs; nothing to do.
        return request

    rewritten: list[dict[str, Any]] = []
    for item in request.media_items:
        url = str(item.get("url", ""))
        if is_provider_fetchable_url(url):
            rewritten.append(dict(item))
            continue
        key = storage_key_from_media_url(url, public_base_url=public_base_url)
        if not key:
            logger.warning("media_staging_missing_key", url=url)
            rewritten.append(dict(item))
            continue
        head = await storage.head_object("public", key)
        if not head.exists:
            logger.warning("media_staging_missing_object", key=key)
            rewritten.append(dict(item))
            continue
        body = await storage.get_object("public", key)
        content_type = head.content_type or "application/octet-stream"
        filename = key.rsplit("/", 1)[-1] or "media.bin"
        public_url = await provider.stage_media(
            filename=filename,
            content_type=content_type,
            body=body,
        )
        logger.info(
            "media_staged_for_publish",
            key=key,
            public_url=public_url,
            bytes=len(body),
        )
        rewritten.append({**item, "url": public_url})

    return request.model_copy(update={"media_items": rewritten})

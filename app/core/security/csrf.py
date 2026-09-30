from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.errors import envelope

logger = structlog.get_logger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


class CSRFMiddleware(BaseHTTPMiddleware):
    """Layer two of three (architecture/05 §5) — `SameSite=Lax` on the session
    cookie is layer one, `Content-Type: application/json` required on every
    mutating endpoint is layer three. Reads the same `cors.allowed_origins`
    list CORS itself uses, so the two allowlists can never silently diverge.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in _SAFE_METHODS:
            # Inbound provider webhooks have no browser Origin; signature
            # verification is the auth layer (architecture/05 §8, Phase 9).
            if request.url.path.startswith("/v1/webhooks/"):
                return await call_next(request)
            origin = request.headers.get("origin") or request.headers.get("referer")
            allowed_origins = get_settings().cors.allowed_origins
            if origin is None or _origin_of(origin) not in allowed_origins:
                request_id = getattr(request.state, "request_id", None)
                logger.warning(
                    "csrf_rejected", reason="csrf_origin_mismatch", request_id=request_id
                )
                return JSONResponse(
                    status_code=403,
                    content=envelope("FORBIDDEN", "Origin not allowed", None, request_id),
                )
        return await call_next(request)

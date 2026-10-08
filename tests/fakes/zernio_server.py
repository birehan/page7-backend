"""Local fake Zernio HTTP API for exercising 401/402/429 without the real vendor.

Usage with an injected httpx client (preferred for unit tests)::

    app = create_fake_zernio_app(default_mode="429")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as http:
        z = ZernioClient(
            alias="t1", api_key="sk_test", base_url="http://test", client=http
        )
        await z.verify_auth()  # raises ProviderRateLimitedError

Mode is taken from the ``X-Fake-Zernio-Mode`` request header when present,
otherwise from ``default_mode`` passed to ``create_fake_zernio_app``.
Valid modes: ``ok``, ``401``, ``402``, ``429``, ``5xx``,
``existing``, ``duplicate``, ``accepted``,
``analytics_delta``, ``analytics_empty``, ``analytics_cursor_expired``,
``analytics_503``, ``inbox_ok``, ``inbox_in_flight``, ``inbox_conflict``,
``inbox_5xx``.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_MODE_HEADER = "X-Fake-Zernio-Mode"
_VALID_MODES = frozenset(
    {
        "ok",
        "401",
        "402",
        "429",
        "5xx",
        "existing",
        "duplicate",
        "accepted",
        "analytics_delta",
        "analytics_empty",
        "analytics_cursor_expired",
        "analytics_503",
        "inbox_ok",
        "inbox_in_flight",
        "inbox_conflict",
        "inbox_5xx",
    }
)


def _mode(request: Request) -> str:
    header = request.headers.get(_MODE_HEADER)
    if header is not None and header in _VALID_MODES:
        return header
    default: str = request.app.state.default_mode
    return default


def _rate_limit_headers(*, remaining: int = 599) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": "600",
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": "1700000000",
    }


def _error_envelope(
    *,
    status: int,
    error: str,
    err_type: str,
    code: str,
    details: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"error": error, "type": err_type, "code": code}
    if details is not None:
        body["details"] = details
    headers = {**_rate_limit_headers(remaining=0 if status == 429 else 599)}
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(body, status_code=status, headers=headers)


def _maybe_error(request: Request) -> Response | None:
    mode = _mode(request)
    if mode in {
        "ok",
        "existing",
        "duplicate",
        "accepted",
        "analytics_delta",
        "analytics_empty",
        "analytics_cursor_expired",
        "analytics_503",
        "inbox_ok",
        "inbox_in_flight",
        "inbox_conflict",
        "inbox_5xx",
    }:
        return None
    if mode == "401":
        return _error_envelope(
            status=401,
            error="Invalid API key",
            err_type="authentication_error",
            code="INVALID_API_KEY",
        )
    if mode == "402":
        return _error_envelope(
            status=402,
            error="Payment required",
            err_type="invalid_request_error",
            code="PAYMENT_REQUIRED",
            details={"reason": "free_tier_exceeded"},
        )
    if mode == "429":
        return _error_envelope(
            status=429,
            error="Rate limit exceeded",
            err_type="rate_limit_error",
            code="RATE_LIMIT",
            extra_headers={"Retry-After": "2"},
        )
    if mode == "5xx":
        return _error_envelope(
            status=503,
            error="Internal error",
            err_type="api_error",
            code="INTERNAL",
        )
    return None


async def auth_verify(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    return JSONResponse(
        {
            "valid": True,
            "userId": "fake-user-1",
            "authType": "api_key",
            "scope": "full",
        },
        headers=_rate_limit_headers(),
    )


async def accounts_health(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    return JSONResponse(
        [
            {
                "accountId": "acc_fake_1",
                "status": "healthy",
                "canPost": True,
                "canFetchAnalytics": True,
                "tokenValid": True,
                "tokenExpiresAt": None,
                "needsReconnect": False,
                "issues": [],
            }
        ],
        headers=_rate_limit_headers(),
    )


async def connect(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    platform = request.path_params.get("platform", "instagram")
    return JSONResponse(
        {
            "authUrl": f"https://example.com/oauth/{platform}",
            "state": "fake-oauth-state",
        },
        headers=_rate_limit_headers(),
    )


async def validate_post(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    return JSONResponse(
        {"valid": True, "errors": [], "warnings": []},
        headers=_rate_limit_headers(),
    )


async def create_post(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    mode = _mode(request)
    body = await request.json()
    request.app.state.create_calls.append(
        {
            "headers": dict(request.headers),
            "body": body,
        }
    )
    # Real client sends Idempotency-Key (pub: keys are not UUIDs, so not
    # x-request-id). Keep x-request-id for older callers / mode scripts.
    idem = request.headers.get("idempotency-key") or request.headers.get(
        "x-request-id"
    )
    if idem and idem in request.app.state.posts_by_xrid:
        stored = request.app.state.posts_by_xrid[idem]
        return JSONResponse(
            {"existingPost": stored},
            status_code=200,
            headers=_rate_limit_headers(),
        )
    if mode == "existing":
        stored = {
            "_id": "z_existing_1",
            "platforms": [
                {
                    "platform": "instagram",
                    "accountId": "acc_fake_1",
                    "status": "published",
                    "platformPostId": "ext_1",
                    "platformPostUrl": "https://instagram.test/p/1",
                }
            ],
        }
        return JSONResponse(
            {"existingPost": stored},
            status_code=200,
            headers=_rate_limit_headers(),
        )
    if mode == "duplicate":
        return _error_envelope(
            status=409,
            error="Duplicate content",
            err_type="invalid_request_error",
            code="DUPLICATE",
            details={"existingPostId": "z_other_post"},
        )

    post_id = f"z_{len(request.app.state.posts) + 1}"
    platforms_in = body.get("platforms") if isinstance(body, dict) else None
    account_id = "acc_fake_1"
    platform = "instagram"
    if isinstance(platforms_in, list) and platforms_in:
        first = platforms_in[0]
        if isinstance(first, dict):
            account_id = str(first.get("accountId") or account_id)
            platform = str(first.get("platform") or platform)
    platform_status = "publishing" if mode == "accepted" else "published"
    stored = {
        "_id": post_id,
        "platforms": [
            {
                "platform": platform,
                "accountId": account_id,
                "status": platform_status,
                "platformPostId": "ext_new" if platform_status == "published" else None,
                "platformPostUrl": (
                    "https://instagram.test/p/new"
                    if platform_status == "published"
                    else None
                ),
            }
        ],
        "metadata": body.get("metadata") if isinstance(body, dict) else {},
    }
    request.app.state.posts[post_id] = stored
    if idem:
        request.app.state.posts_by_xrid[idem] = stored
    return JSONResponse(stored, status_code=201, headers=_rate_limit_headers())


async def get_post(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    post_id = request.path_params["post_id"]
    stored = request.app.state.posts.get(post_id)
    if stored is None:
        return _error_envelope(
            status=404,
            error="Not found",
            err_type="invalid_request_error",
            code="NOT_FOUND",
        )
    # Resolve in-flight platforms for reconciliation.
    platforms = stored.get("platforms")
    if isinstance(platforms, list):
        for row in platforms:
            if isinstance(row, dict) and row.get("status") in (
                "pending",
                "publishing",
            ):
                row["status"] = "published"
                row["platformPostId"] = row.get("platformPostId") or "ext_resolved"
                row["platformPostUrl"] = (
                    row.get("platformPostUrl") or "https://instagram.test/p/resolved"
                )
    return JSONResponse(stored, headers=_rate_limit_headers())


async def analytics_delta(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    mode = _mode(request)
    if mode == "402":
        return _error_envelope(
            status=402,
            error="Analytics add-on required",
            err_type="invalid_request_error",
            code="analytics_addon_required",
        )
    if mode == "analytics_503":
        return _error_envelope(
            status=503,
            error="Analytics temporarily unavailable",
            err_type="api_error",
            code="UNAVAILABLE",
            extra_headers={"Retry-After": "3"},
        )
    if mode == "analytics_cursor_expired":
        return _error_envelope(
            status=400,
            error="Cursor expired",
            err_type="invalid_request_error",
            code="CURSOR_EXPIRED",
        )
    if mode == "analytics_empty":
        return JSONResponse(
            {
                "data": [],
                "nextCursor": request.query_params.get("cursor") or "cursor_bootstrap",
                "hasMore": False,
            },
            headers=_rate_limit_headers(),
        )
    # analytics_delta / ok default — non-empty page
    return JSONResponse(
        {
            "data": [
                {
                    "postId": "zp_delta_1",
                    "accountId": "acc_fake_1",
                    "profileId": "prof_fake_1",
                    "platform": "instagram",
                    "platformPostId": "ext_1",
                    "publishedAt": "2024-11-01T10:00:05Z",
                    "syncedAt": "2024-11-02T08:30:00Z",
                    "isDeleted": False,
                    "metrics": {"impressions": 15420, "likes": 342},
                }
            ],
            "nextCursor": "cursor_after_1",
            "hasMore": False,
        },
        headers=_rate_limit_headers(),
    )


async def analytics_baseline(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    mode = _mode(request)
    if mode == "402":
        return _error_envelope(
            status=402,
            error="Analytics add-on required",
            err_type="invalid_request_error",
            code="analytics_addon_required",
        )
    if mode == "analytics_503":
        return _error_envelope(
            status=503,
            error="Analytics temporarily unavailable",
            err_type="api_error",
            code="UNAVAILABLE",
            extra_headers={"Retry-After": "3"},
        )
    return JSONResponse(
        {
            "hasAnalyticsAccess": True,
            "accounts": [
                {
                    "accountId": "acc_fake_1",
                    "platform": "instagram",
                    "followerCount": 1200,
                }
            ],
            "posts": [
                {
                    "postId": "zp_base_1",
                    "accountId": "acc_fake_1",
                    "platform": "instagram",
                    "publishedAt": "2024-11-01T10:00:05Z",
                    "analytics": {"impressions": 100, "likes": 10},
                }
            ],
        },
        headers=_rate_limit_headers(),
    )


async def inbox_conversations(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    return JSONResponse(
        {
            "conversations": [
                {
                    "_id": "conv_fake_1",
                    "accountId": request.query_params.get("accountId") or "acc_fake_1",
                    "platform": "instagram",
                    "status": "open",
                    "participantName": "Ada",
                    "participantHandle": "@ada",
                    "lastMessageAt": "2024-11-02T08:00:00Z",
                    "unreadCount": 1,
                }
            ],
            "nextCursor": None,
            "hasMore": False,
        },
        headers=_rate_limit_headers(),
    )


async def inbox_comments(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    post_id = request.path_params["post_id"]
    account_id = request.query_params.get("accountId") or "acc_fake_1"
    return JSONResponse(
        [
            {
                "_id": "cmt_fake_1",
                "postId": post_id,
                "accountId": account_id,
                "message": "Nice post!",
                "authorName": "Ada",
                "authorHandle": "@ada",
                "createdAt": "2024-11-02T09:00:00Z",
            }
        ],
        headers=_rate_limit_headers(),
    )


async def list_inbox_messages(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    conversation_id = request.path_params["conversation_id"]
    account_id = request.query_params.get("accountId") or "acc_fake_1"
    return JSONResponse(
        {
            "messages": [
                {
                    "_id": "msg_fake_1",
                    "conversationId": conversation_id,
                    "accountId": account_id,
                    "direction": "incoming",
                    "text": "Hello there",
                    "senderName": "Ada",
                    "senderHandle": "@ada",
                    "timestamp": "2024-11-02T08:00:00Z",
                },
                {
                    "_id": "msg_fake_2",
                    "conversationId": conversation_id,
                    "accountId": account_id,
                    "direction": "outgoing",
                    "text": "Thanks for reaching out!",
                    "senderName": "Brand",
                    "timestamp": "2024-11-02T08:05:00Z",
                },
            ],
            "pagination": {"hasMore": False, "nextCursor": None},
        },
        headers=_rate_limit_headers(),
    )


def _idempotency_replay_or_store(
    request: Request, *, key: str | None, status: int, body: dict[str, Any]
) -> Response | None:
    """Return a replay response if key already stored with 2xx; else None."""
    store: dict[str, dict[str, Any]] = request.app.state.idempotency_store
    if not key:
        return None
    prior = store.get(key)
    if prior is not None:
        return JSONResponse(
            prior["body"],
            status_code=int(prior["status"]),
            headers={
                **_rate_limit_headers(),
                "Idempotent-Replayed": "true",
            },
        )
    if 200 <= status < 300:
        store[key] = {"status": status, "body": body}
    # Non-2xx deliberately does not store — key is released.
    return None


async def send_inbox_message(request: Request) -> Response:
    err = _maybe_error(request)
    if err is not None:
        return err
    mode = _mode(request)
    key = request.headers.get("Idempotency-Key")
    body_in = await request.json()
    request.app.state.inbox_send_calls.append(
        {"headers": dict(request.headers), "body": body_in, "path": str(request.url.path)}
    )

    if mode == "inbox_in_flight":
        return _error_envelope(
            status=409,
            error="Request in flight",
            err_type="invalid_request_error",
            code="IN_FLIGHT",
        )
    if mode == "inbox_conflict":
        return _error_envelope(
            status=422,
            error="Idempotency key conflict",
            err_type="invalid_request_error",
            code="IDEMPOTENCY_CONFLICT",
        )
    if mode == "inbox_5xx":
        # 5xx does not store the key.
        return _error_envelope(
            status=503,
            error="Internal error",
            err_type="api_error",
            code="INTERNAL",
        )

    response_body = {
        "_id": f"msg_{len(request.app.state.inbox_send_calls)}",
        "messageId": f"msg_{len(request.app.state.inbox_send_calls)}",
        "accountId": body_in.get("accountId") if isinstance(body_in, dict) else None,
        "message": body_in.get("message") if isinstance(body_in, dict) else None,
    }
    replay = _idempotency_replay_or_store(
        request, key=key, status=201, body=response_body
    )
    if replay is not None:
        return replay
    return JSONResponse(response_body, status_code=201, headers=_rate_limit_headers())


async def reply_inbox_comment(request: Request) -> Response:
    return await send_inbox_message(request)


def create_fake_zernio_app(*, default_mode: str = "ok") -> Starlette:
    """Build a Starlette app that mimics a subset of Zernio's HTTP API."""
    if default_mode not in _VALID_MODES:
        raise ValueError(f"default_mode must be one of {sorted(_VALID_MODES)}")
    app = Starlette(
        routes=[
            Route("/auth/verify", auth_verify, methods=["GET"]),
            Route("/accounts/health", accounts_health, methods=["GET"]),
            Route("/connect/{platform}", connect, methods=["GET"]),
            Route("/tools/validate/post", validate_post, methods=["POST"]),
            Route("/posts", create_post, methods=["POST"]),
            Route("/posts/{post_id}", get_post, methods=["GET"]),
            Route("/analytics/delta", analytics_delta, methods=["GET"]),
            Route("/analytics", analytics_baseline, methods=["GET"]),
            Route("/inbox/conversations", inbox_conversations, methods=["GET"]),
            Route("/inbox/comments/{post_id}", inbox_comments, methods=["GET"]),
            Route(
                "/inbox/conversations/{conversation_id}/messages",
                list_inbox_messages,
                methods=["GET"],
            ),
            Route(
                "/inbox/conversations/{conversation_id}/messages",
                send_inbox_message,
                methods=["POST"],
            ),
            Route(
                "/inbox/comments/{post_id}",
                reply_inbox_comment,
                methods=["POST"],
            ),
        ]
    )
    app.state.default_mode = default_mode
    app.state.posts = {}
    app.state.posts_by_xrid = {}
    app.state.create_calls = []
    app.state.idempotency_store = {}
    app.state.inbox_send_calls = []
    return app

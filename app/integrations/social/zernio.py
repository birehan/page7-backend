"""Zernio HTTP adapter — the only module that talks to zernio.com.

Disabled-credential checks belong to CredentialPool / the social_accounts
service layer: this client never hits the DB. Callers must refuse network I/O
for credentials with status='disabled' before constructing or calling this
client (architecture/10 §2).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx

from app.infrastructure.telemetry.metrics import provider_call
from app.integrations.errors import (
    AnalyticsCursorExpired,
    ProviderAuthError,
    ProviderError,
    ProviderNotFoundError,
    ProviderPaymentRequiredError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)
from app.integrations.social.ports import (
    AccountHealth,
    AccountInfo,
    AnalyticsBaselineAccount,
    AnalyticsBaselinePost,
    AnalyticsBaselineResult,
    AnalyticsDeltaEntry,
    AnalyticsDeltaResult,
    AuthVerifyResult,
    ConnectUrlResult,
    InboxComment,
    InboxConversation,
    InboxConversationsResult,
    InboxMessage,
    InboxMessagesResult,
    InboxSendResult,
    PlatformOutcome,
    ProfileInfo,
    PublishRequest,
    PublishResult,
    RateLimitInfo,
    ValidatePostResult,
)

_DEFAULT_BASE = "https://zernio.com/api/v1"
_DEFAULT_CONCURRENCY = 4
_ANALYTICS_MIN_INTERVAL_S = 1.0

RateLimitCallback = Callable[[str, RateLimitInfo], Awaitable[None] | None]


class ZernioClient:
    """httpx AsyncClient-based SocialProvider for one Zernio API key (alias)."""

    _semaphores: dict[str, asyncio.Semaphore] = {}

    def __init__(
        self,
        *,
        alias: str,
        api_key: str,
        base_url: str = _DEFAULT_BASE,
        concurrency: int = _DEFAULT_CONCURRENCY,
        on_rate_limit: RateLimitCallback | None = None,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        analytics_min_interval: float = _ANALYTICS_MIN_INTERVAL_S,
    ) -> None:
        self.alias = alias
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._on_rate_limit = on_rate_limit
        self._timeout = timeout
        self._client = client
        if alias not in ZernioClient._semaphores:
            ZernioClient._semaphores[alias] = asyncio.Semaphore(concurrency)
        self._semaphore = ZernioClient._semaphores[alias]
        self._analytics_lock = asyncio.Lock()
        self._last_analytics_at: float | None = None
        self._analytics_min_interval = analytics_min_interval

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def verify_auth(self) -> AuthVerifyResult:
        payload = _as_object(await self._request("GET", "/auth/verify"))
        return AuthVerifyResult(
            valid=bool(payload.get("valid", False)),
            user_id=_str_or_none(payload.get("userId")),
            auth_type=_str_or_none(payload.get("authType")),
            scope=_str_or_none(payload.get("scope")),
        )

    async def list_profiles(self) -> list[ProfileInfo]:
        payload = await self._request("GET", "/profiles")
        rows = _as_list(payload)
        return [_map_profile(row) for row in rows]

    async def create_profile(
        self, name: str, description: str | None = None
    ) -> ProfileInfo:
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        try:
            payload = _as_object(await self._request("POST", "/profiles", json=body))
        except ProviderError as exc:
            # Idempotent create: a prior success that failed to persist locally
            # leaves a name conflict — reuse the existing profile.
            if "profile_name_conflict" not in str(exc):
                raise
            for existing in await self.list_profiles():
                if existing.name == name:
                    return existing
            raise
        nested = payload.get("profile")
        if isinstance(nested, dict):
            return _map_profile(nested)
        return _map_profile(payload)

    async def get_connect_url(
        self,
        platform: str,
        profile_id: str,
        redirect_url: str,
        *,
        login_method: str | None = None,
    ) -> ConnectUrlResult:
        params: dict[str, str] = {
            "profileId": profile_id,
            "redirect_url": redirect_url,
        }
        if login_method is not None:
            params["loginMethod"] = login_method
        payload = _as_object(
            await self._request("GET", f"/connect/{platform}", params=params)
        )
        auth_url = payload.get("authUrl") or payload.get("auth_url")
        state = payload.get("state")
        if not isinstance(auth_url, str) or not isinstance(state, str):
            raise ProviderUnavailableError("connect URL response missing authUrl/state")
        return ConnectUrlResult(auth_url=auth_url, provider_state=state)

    async def list_accounts(
        self, *, profile_id: str | None = None, platform: str | None = None
    ) -> list[AccountInfo]:
        params: dict[str, str] = {}
        if profile_id is not None:
            params["profileId"] = profile_id
        if platform is not None:
            params["platform"] = platform
        payload = await self._request("GET", "/accounts", params=params or None)
        rows = _as_list(payload)
        return [_map_account(row) for row in rows]

    async def get_accounts_health(
        self, *, profile_id: str | None = None
    ) -> list[AccountHealth]:
        params: dict[str, str] = {}
        if profile_id is not None:
            params["profileId"] = profile_id
        payload = await self._request(
            "GET", "/accounts/health", params=params or None
        )
        rows = _as_list(payload)
        return [_map_health(row) for row in rows]

    async def delete_account(self, account_id: str) -> None:
        """DELETE /v1/accounts/{id}. 404 = already gone (idempotent success)."""
        try:
            await self._request("DELETE", f"/accounts/{account_id}")
        except ProviderNotFoundError:
            return

    async def validate_post(self, payload: dict[str, Any]) -> ValidatePostResult:
        body = _as_object(
            await self._request("POST", "/tools/validate/post", json=payload)
        )
        errors = body.get("errors") or []
        warnings = body.get("warnings") or []
        return ValidatePostResult(
            valid=bool(body.get("valid", False)),
            errors=[str(e) for e in errors] if isinstance(errors, list) else [],
            warnings=[str(w) for w in warnings] if isinstance(warnings, list) else [],
        )

    async def publish(self, request: PublishRequest) -> PublishResult:
        """POST /v1/posts with publishNow:true; normalize every outcome shape.

        Callers: publish_post handler via SocialProvider.publish.
        User: post now taking forever / error while actually posted.
        Zernio: Idempotency-Key for timeout retries; publishNow is sync (can
        exceed 30s on Instagram). x-request-id must be UUID — our pub: keys
        are not, so send Idempotency-Key only.
        """
        body: dict[str, Any] = {
            "content": request.content,
            "platforms": request.platforms,
            "publishNow": True,
            "metadata": dict(request.metadata),
        }
        if request.media_items:
            body["mediaItems"] = list(request.media_items)
        if request.title is not None:
            body["title"] = request.title
        try:
            status, payload, headers = await self._request_raw(
                "POST",
                "/posts",
                json=body,
                extra_headers={
                    "Idempotency-Key": request.idempotency_key[:255],
                },
                timeout=90.0,
            )
        except ProviderUnavailableError as exc:
            return PublishResult(
                kind="non_definitive",
                error_message=str(exc),
            )
        return _normalize_publish_response(status, payload, headers)

    async def stage_media(
        self,
        *,
        filename: str,
        content_type: str,
        body: bytes,
    ) -> str:
        """Upload bytes via Zernio presign; return a public HTTPS URL for publish.

        Used when our own media URL is not reachable by Zernio (LocalStorage
        localhost in development). Temp objects expire after ~7 days; publish
        promptly after staging.
        """
        payload = _as_object(
            await self._request(
                "POST",
                "/media/presign",
                json={
                    "filename": filename,
                    "contentType": content_type,
                    "size": len(body),
                },
            )
        )
        upload_url = payload.get("uploadUrl")
        public_url = payload.get("publicUrl")
        if not isinstance(upload_url, str) or not isinstance(public_url, str):
            raise ProviderUnavailableError("zernio media presign missing upload/public URL")
        try:
            if self._client is not None:
                response = await self._client.put(
                    upload_url,
                    content=body,
                    headers={"Content-Type": content_type},
                )
            else:
                async with httpx.AsyncClient(timeout=max(self._timeout, 60.0)) as client:
                    response = await client.put(
                        upload_url,
                        content=body,
                        headers={"Content-Type": content_type},
                    )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"zernio media upload failed: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"zernio media upload rejected status={response.status_code}"
            )
        return public_url

    async def get_post(self, zernio_post_id: str) -> PublishResult:
        """GET /v1/posts/{id} for reconciliation's definitive-resolution path."""
        try:
            status, payload, headers = await self._request_raw(
                "GET", f"/posts/{zernio_post_id}"
            )
        except ProviderUnavailableError as exc:
            return PublishResult(
                kind="non_definitive",
                error_message=str(exc),
            )
        return _normalize_publish_response(status, payload, headers)

    async def get_analytics_delta(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> AnalyticsDeltaResult:
        async with provider_call(provider="zernio", operation="analytics_delta"):
            await self._pace_analytics()
            params: dict[str, str] = {"limit": str(limit)}
            if cursor is not None:
                params["cursor"] = cursor
            status, payload, headers = await self._request_raw(
                "GET", "/analytics/delta", params=params
            )
            if status == 400:
                raise AnalyticsCursorExpired(
                    f"zernio analytics cursor expired status={status}"
                )
            if status == 402:
                raise ProviderPaymentRequiredError(
                    "zernio analytics addon required"
                )
            if status == 503:
                raise ProviderUnavailableError(
                    f"zernio analytics unavailable status={status}",
                    retry_after=_retry_after_headers(headers),
                )
            if status >= 400:
                # Build a temporary response so existing mapper applies.
                fake = httpx.Response(
                    status_code=status,
                    json=payload if isinstance(payload, dict) else {},
                    headers=headers,
                    request=httpx.Request("GET", f"{self._base_url}/analytics/delta"),
                )
                _raise_for_status(fake)
            return _map_analytics_delta(_as_object(payload or {}))

    async def get_analytics_baseline(self, *, limit: int = 50) -> AnalyticsBaselineResult:
        async with provider_call(provider="zernio", operation="analytics_baseline"):
            await self._pace_analytics()
            status, payload, headers = await self._request_raw(
                "GET", "/analytics", params={"limit": str(limit)}
            )
            if status == 402:
                raise ProviderPaymentRequiredError(
                    "zernio analytics addon required"
                )
            if status == 503:
                raise ProviderUnavailableError(
                    f"zernio analytics unavailable status={status}",
                    retry_after=_retry_after_headers(headers),
                )
            if status >= 400:
                fake = httpx.Response(
                    status_code=status,
                    json=payload if isinstance(payload, dict) else {},
                    headers=headers,
                    request=httpx.Request("GET", f"{self._base_url}/analytics"),
                )
                _raise_for_status(fake)
            return _map_analytics_baseline(_as_object(payload or {}))

    async def list_inbox_conversations(
        self,
        *,
        account_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> InboxConversationsResult:
        async with provider_call(provider="zernio", operation="inbox_sync"):
            params: dict[str, str] = {"limit": str(limit)}
            if account_id is not None:
                params["accountId"] = account_id
            if cursor is not None:
                params["cursor"] = cursor
            payload = await self._request(
                "GET", "/inbox/conversations", params=params
            )
            if isinstance(payload, list):
                return _map_inbox_conversations({"conversations": payload})
            return _map_inbox_conversations(_as_object(payload))

    async def list_inbox_messages(
        self,
        *,
        conversation_id: str,
        account_id: str,
        limit: int = 100,
        cursor: str | None = None,
        sort_order: str = "asc",
    ) -> InboxMessagesResult:
        async with provider_call(provider="zernio", operation="inbox_sync"):
            params: dict[str, str] = {
                "accountId": account_id,
                "limit": str(limit),
                "sortOrder": sort_order,
            }
            if cursor is not None:
                params["cursor"] = cursor
            payload = await self._request(
                "GET",
                f"/inbox/conversations/{conversation_id}/messages",
                params=params,
            )
            return _map_inbox_messages(
                _as_object(payload) if not isinstance(payload, list) else {"data": payload},
                conversation_id=conversation_id,
                account_id=account_id,
            )

    async def get_post_comments(
        self, *, post_id: str, account_id: str
    ) -> list[InboxComment]:
        async with provider_call(provider="zernio", operation="inbox_sync"):
            payload = await self._request(
                "GET",
                f"/inbox/comments/{post_id}",
                params={"accountId": account_id},
            )
            rows = _as_list(payload)
            return [_map_inbox_comment(row, post_id=post_id, account_id=account_id) for row in rows]

    async def send_inbox_message(
        self,
        *,
        conversation_id: str,
        account_id: str,
        message: str,
        idempotency_key: str,
    ) -> InboxSendResult:
        async with provider_call(provider="zernio", operation="inbox_reply_send"):
            return await self._inbox_send(
                "POST",
                f"/inbox/conversations/{conversation_id}/messages",
                body={"accountId": account_id, "message": message},
                idempotency_key=idempotency_key,
            )

    async def reply_to_post_comment(
        self,
        *,
        post_id: str,
        account_id: str,
        message: str,
        idempotency_key: str,
        comment_id: str | None = None,
    ) -> InboxSendResult:
        async with provider_call(provider="zernio", operation="inbox_reply_send"):
            body: dict[str, Any] = {"accountId": account_id, "message": message}
            if comment_id is not None:
                body["commentId"] = comment_id
            return await self._inbox_send(
                "POST",
                f"/inbox/comments/{post_id}",
                body=body,
                idempotency_key=idempotency_key,
            )

    async def _inbox_send(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any],
        idempotency_key: str,
    ) -> InboxSendResult:
        try:
            status, payload, headers = await self._request_raw(
                method,
                path,
                json=body,
                extra_headers={"Idempotency-Key": idempotency_key},
            )
        except ProviderUnavailableError as exc:
            return InboxSendResult(
                kind="ambiguous",
                http_status=None,
                error_message=str(exc),
            )

        return _normalize_inbox_send(status, payload, headers)

    async def _pace_analytics(self) -> None:
        """Optional 1s spacing between analytics calls on this client."""
        async with self._analytics_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._last_analytics_at is not None:
                wait = self._analytics_min_interval - (now - self._last_analytics_at)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_analytics_at = loop.time()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[Any]:
        response = await self._send(
            method,
            path,
            params=params,
            json=json,
            extra_headers=extra_headers,
        )
        await self._emit_rate_limit(response)
        if response.status_code >= 400:
            _raise_for_status(response)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            data: Any = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError("invalid JSON from Zernio") from exc
        if isinstance(data, (dict, list)):
            return data
        raise ProviderUnavailableError("unexpected Zernio response shape")

    async def _request_raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None, httpx.Headers]:
        """Like ``_request`` but returns status/body for publish outcome decoding."""
        response = await self._send(
            method,
            path,
            params=params,
            json=json,
            extra_headers=extra_headers,
            timeout=timeout,
        )
        await self._emit_rate_limit(response)
        if response.status_code == 204 or not response.content:
            return response.status_code, None, response.headers
        try:
            data: Any = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError("invalid JSON from Zernio") from exc
        if isinstance(data, (dict, list)):
            return response.status_code, data, response.headers
        raise ProviderUnavailableError("unexpected Zernio response shape")

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        request_timeout = timeout if timeout is not None else self._timeout
        async with self._semaphore:
            try:
                if self._client is not None:
                    return await self._client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json,
                        timeout=request_timeout,
                    )
                async with httpx.AsyncClient(timeout=request_timeout) as client:
                    return await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json,
                    )
            except httpx.TimeoutException as exc:
                raise ProviderUnavailableError(str(exc)) from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(str(exc)) from exc

    async def _emit_rate_limit(self, response: httpx.Response) -> None:
        info = _parse_rate_limit(response)
        if self._on_rate_limit is None:
            return
        result = self._on_rate_limit(self.alias, info)
        if asyncio.iscoroutine(result):
            await result


def _parse_rate_limit(response: httpx.Response) -> RateLimitInfo:
    def _int(name: str) -> int | None:
        raw = response.headers.get(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    return RateLimitInfo(
        limit=_int("X-RateLimit-Limit"),
        remaining=_int("X-RateLimit-Remaining"),
        reset_at_unix=_int("X-RateLimit-Reset"),
    )


def _payment_reason(body: dict[str, Any] | None) -> str | None:
    """Extract Zernio 402 ``reason`` from body or nested details."""
    if not isinstance(body, dict):
        return None
    details = body.get("details")
    if isinstance(details, dict):
        reason = details.get("reason")
        if isinstance(reason, str) and reason:
            return reason
    reason = body.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    return None


def _raise_for_status(response: httpx.Response) -> None:
    """Map HTTP status and Zernio type/code — never branch on message text."""
    body = _safe_json(response)
    err_type = body.get("type") if isinstance(body, dict) else None
    code = body.get("code") if isinstance(body, dict) else None
    status = response.status_code

    if status == 401 or err_type == "authentication_error":
        raise ProviderAuthError(f"zernio auth failed code={code!r}")
    if status == 404:
        raise ProviderNotFoundError(
            f"zernio not found status={status} type={err_type!r} code={code!r}"
        )
    if status == 402 or code == "PAYMENT_REQUIRED":
        reason = _payment_reason(body if isinstance(body, dict) else None)
        raise ProviderPaymentRequiredError(
            f"zernio payment required code={code!r} reason={reason!r}",
            reason=reason,
        )
    if status == 429 or err_type == "rate_limit_error":
        raise ProviderRateLimitedError(retry_after=_retry_after(response))
    if status >= 500 or err_type == "api_error":
        raise ProviderUnavailableError(
            f"zernio unavailable status={status} type={err_type!r} code={code!r}",
            retry_after=_retry_after(response),
        )
    raise ProviderError(
        f"zernio error status={status} type={err_type!r} code={code!r}"
    )


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _safe_json(response: httpx.Response) -> dict[str, Any] | list[Any] | None:
    try:
        data: Any = response.json()
    except ValueError:
        return None
    if isinstance(data, (dict, list)):
        return data
    return None


def _as_object(payload: dict[str, Any] | list[Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    raise ProviderUnavailableError("expected JSON object from Zernio")


def _as_list(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    for key in ("data", "profiles", "accounts", "results"):
        nested = payload.get(key)
        if isinstance(nested, list):
            return [row for row in nested if isinstance(row, dict)]
    return []

def _map_profile(row: dict[str, Any]) -> ProfileInfo:
    profile_id = row.get("_id") or row.get("id")
    if not isinstance(profile_id, str):
        raise ProviderUnavailableError("profile missing id")
    name = row.get("name")
    return ProfileInfo(
        id=profile_id,
        name=str(name) if name is not None else "",
        description=_str_or_none(row.get("description")),
    )


def _map_account(row: dict[str, Any]) -> AccountInfo:
    account_id = row.get("_id") or row.get("id")
    if not isinstance(account_id, str):
        raise ProviderUnavailableError("account missing id")
    return AccountInfo(
        id=account_id,
        platform=str(row.get("platform") or ""),
        profile_id=str(row.get("profileId") or ""),
        username=str(row.get("username") or ""),
        display_name=_str_or_none(row.get("displayName")),
        profile_picture=_str_or_none(row.get("profilePicture")),
        is_active=bool(row.get("isActive", True)),
        needs_reconnection=bool(row.get("needsReconnection", False)),
    )


def _map_health(row: dict[str, Any]) -> AccountHealth:
    account_id = row.get("accountId") or row.get("_id") or row.get("id")
    issues_raw = row.get("issues") or []
    issues = [str(i) for i in issues_raw] if isinstance(issues_raw, list) else []
    expires = row.get("tokenExpiresAt")
    return AccountHealth(
        account_id=str(account_id or ""),
        status=str(row.get("status") or "error"),
        can_post=bool(row.get("canPost", False)),
        can_fetch_analytics=bool(row.get("canFetchAnalytics", False)),
        token_valid=bool(row.get("tokenValid", False)),
        token_expires_at=str(expires) if expires is not None else None,
        needs_reconnect=bool(row.get("needsReconnect", False)),
        issues=issues,
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _normalize_publish_response(
    status: int,
    payload: dict[str, Any] | list[Any] | None,
    headers: httpx.Headers,
) -> PublishResult:
    """Map Zernio HTTP outcomes onto PublishResult (architecture/10 §6)."""
    body = payload if isinstance(payload, dict) else {}
    raw = body if body else None

    if status == 401:
        return PublishResult(
            kind="auth_failed",
            http_status=status,
            error_message=_error_message(body),
            raw_response=raw,
        )
    if status == 402:
        return PublishResult(
            kind="payment_required",
            http_status=status,
            error_message=_error_message(body),
            raw_response=raw,
        )
    if status == 429:
        return PublishResult(
            kind="rate_limited",
            http_status=status,
            retry_after=_retry_after_headers(headers),
            error_message=_error_message(body),
            raw_response=raw,
        )
    if status == 409:
        details = body.get("details") if isinstance(body.get("details"), dict) else {}
        existing = details.get("existingPostId") if isinstance(details, dict) else None
        return PublishResult(
            kind="duplicate_conflict",
            existing_post_id=str(existing) if existing is not None else None,
            http_status=status,
            error_message=_error_message(body),
            raw_response=raw,
        )
    if status >= 500 or status == 0:
        return PublishResult(
            kind="non_definitive",
            http_status=status,
            error_message=_error_message(body) or f"upstream status {status}",
            raw_response=raw,
        )
    if status >= 400:
        return PublishResult(
            kind="rejected",
            http_status=status,
            error_category=_first_error_category(body),
            error_message=_error_message(body),
            raw_response=raw,
        )

    # 200 / 201 success paths — including 200 + existingPost (idempotent replay).
    existing_post = body.get("existingPost")
    if isinstance(existing_post, dict):
        post_obj = existing_post
        outcome_kind: Literal["created", "existing"] = "existing"
    else:
        nested = body.get("post") or body.get("data")
        post_obj = nested if isinstance(nested, dict) else body
        outcome_kind = "existing" if status == 200 and "existingPost" in body else "created"

    zernio_id = post_obj.get("_id") or post_obj.get("id")
    platforms = _map_platforms(post_obj.get("platforms"))
    return PublishResult(
        kind=outcome_kind,
        zernio_post_id=str(zernio_id) if zernio_id is not None else None,
        platforms=platforms,
        http_status=status,
        raw_response=raw,
    )


def _account_id_from_platform_row(raw: Any) -> str:
    """Zernio may return accountId as a string or nested `{_id: ...}` object."""
    if isinstance(raw, dict):
        nested = raw.get("_id") or raw.get("id")
        return str(nested) if nested is not None else ""
    if raw is None:
        return ""
    return str(raw)


def _normalize_platform_status(raw: Any) -> str:
    """Map provider platform statuses onto our pending/publishing/published/failed set."""
    status = str(raw or "pending").strip().lower()
    # Zernio Instagram often returns "processing" while the post is already accepted.
    if status in {"processing", "queued", "scheduled", "in_progress", "in-progress"}:
        return "publishing"
    if status in {"success", "succeeded", "complete", "completed", "live"}:
        return "published"
    if status in {"error", "errored"}:
        return "failed"
    return status or "pending"


def _map_platforms(raw: Any) -> list[PlatformOutcome]:
    if not isinstance(raw, list):
        return []
    out: list[PlatformOutcome] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        out.append(
            PlatformOutcome(
                platform=str(row.get("platform") or ""),
                account_id=_account_id_from_platform_row(row.get("accountId")),
                status=_normalize_platform_status(row.get("status")),
                platform_post_id=_str_or_none(row.get("platformPostId")),
                platform_post_url=_str_or_none(row.get("platformPostUrl")),
                error_category=_str_or_none(row.get("errorCategory")),
                error_message=_str_or_none(row.get("errorMessage")),
            )
        )
    return out


def _first_error_category(body: dict[str, Any]) -> str | None:
    platforms = body.get("platforms")
    if isinstance(platforms, list):
        for row in platforms:
            if isinstance(row, dict) and row.get("errorCategory"):
                return str(row["errorCategory"])
    details = body.get("details")
    if isinstance(details, dict) and details.get("errorCategory"):
        return str(details["errorCategory"])
    return _str_or_none(body.get("errorCategory"))


def _error_message(body: dict[str, Any]) -> str | None:
    for key in ("error", "message", "errorMessage"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _retry_after_headers(headers: httpx.Headers) -> float | None:
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _map_analytics_delta(body: dict[str, Any]) -> AnalyticsDeltaResult:
    rows_raw = body.get("data")
    rows = rows_raw if isinstance(rows_raw, list) else []
    entries: list[AnalyticsDeltaEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        metrics_raw = row.get("metrics") or row.get("analytics") or {}
        metrics = metrics_raw if isinstance(metrics_raw, dict) else {}
        entries.append(
            AnalyticsDeltaEntry(
                post_id=str(row.get("postId") or row.get("post_id") or ""),
                account_id=str(row.get("accountId") or row.get("account_id") or ""),
                profile_id=str(row.get("profileId") or row.get("profile_id") or ""),
                platform=str(row.get("platform") or ""),
                platform_post_id=_str_or_none(
                    row.get("platformPostId") or row.get("platform_post_id")
                ),
                published_at=_str_or_none(
                    row.get("publishedAt") or row.get("published_at")
                ),
                synced_at=_str_or_none(row.get("syncedAt") or row.get("synced_at")),
                is_deleted=bool(row.get("isDeleted") or row.get("is_deleted") or False),
                metrics=dict(metrics),
            )
        )
    next_cursor = body.get("nextCursor") or body.get("next_cursor") or ""
    has_more = bool(body.get("hasMore") or body.get("has_more") or False)
    return AnalyticsDeltaResult(
        data=entries,
        next_cursor=str(next_cursor),
        has_more=has_more,
    )


def _map_analytics_baseline(body: dict[str, Any]) -> AnalyticsBaselineResult:
    has_access = body.get("hasAnalyticsAccess")
    if has_access is None:
        has_access = body.get("has_analytics_access")
    if has_access is None:
        has_access = True

    accounts_raw = body.get("accounts")
    accounts: list[AnalyticsBaselineAccount] = []
    if isinstance(accounts_raw, list):
        for row in accounts_raw:
            if not isinstance(row, dict):
                continue
            account_id = (
                row.get("accountId")
                or row.get("account_id")
                or row.get("_id")
                or row.get("id")
            )
            accounts.append(
                AnalyticsBaselineAccount(
                    account_id=str(account_id or ""),
                    platform=str(row.get("platform") or ""),
                    follower_count=_int_or_none(
                        row.get("followerCount")
                        if row.get("followerCount") is not None
                        else row.get("follower_count")
                    ),
                )
            )

    posts_raw = body.get("posts") or body.get("data") or body.get("results")
    posts: list[AnalyticsBaselinePost] = []
    if isinstance(posts_raw, list):
        for row in posts_raw:
            if not isinstance(row, dict):
                continue
            metrics_raw = row.get("metrics") or row.get("analytics") or {}
            metrics = metrics_raw if isinstance(metrics_raw, dict) else {}
            account_id = row.get("accountId") or row.get("account_id")
            if account_id is None:
                platforms = row.get("platformAnalytics")
                if isinstance(platforms, list) and platforms:
                    first = platforms[0]
                    if isinstance(first, dict):
                        account_id = first.get("accountId")
            post_id = (
                row.get("postId")
                or row.get("post_id")
                or row.get("_id")
                or row.get("id")
                or ""
            )
            posts.append(
                AnalyticsBaselinePost(
                    post_id=str(post_id),
                    account_id=str(account_id or ""),
                    platform=str(row.get("platform") or ""),
                    published_at=_str_or_none(
                        row.get("publishedAt") or row.get("published_at")
                    ),
                    metrics=dict(metrics),
                )
            )

    return AnalyticsBaselineResult(
        has_analytics_access=bool(has_access),
        accounts=accounts,
        posts=posts,
    )


def _map_inbox_conversations(body: dict[str, Any]) -> InboxConversationsResult:
    rows_raw = body.get("conversations") or body.get("data") or body.get("items")
    if isinstance(body, list):  # pragma: no cover - typed as dict
        rows_raw = body
    rows = rows_raw if isinstance(rows_raw, list) else []
    conversations: list[InboxConversation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        conv_id = row.get("_id") or row.get("id") or row.get("conversationId")
        conversations.append(
            InboxConversation(
                id=str(conv_id or ""),
                account_id=str(row.get("accountId") or row.get("account_id") or ""),
                platform=str(row.get("platform") or ""),
                status=_str_or_none(row.get("status")),
                participant_name=_str_or_none(
                    row.get("participantName") or row.get("participant_name")
                ),
                participant_handle=_str_or_none(
                    row.get("participantHandle")
                    or row.get("participant_handle")
                    or row.get("participantUsername")
                    or row.get("accountUsername")
                ),
                last_message_at=_str_or_none(
                    row.get("lastMessageAt")
                    or row.get("last_message_at")
                    or row.get("updatedTime")
                    or row.get("updated_time")
                ),
                last_message_preview=_str_or_none(
                    row.get("lastMessage") or row.get("last_message")
                ),
                unread_count=_int_or_none(
                    row.get("unreadCount")
                    if row.get("unreadCount") is not None
                    else row.get("unread_count")
                ),
            )
        )
    pagination: dict[str, Any] = {}
    pagination_raw = body.get("pagination")
    if isinstance(pagination_raw, dict):
        pagination = pagination_raw
    next_cursor = (
        body.get("nextCursor")
        or body.get("next_cursor")
        or pagination.get("nextCursor")
        or pagination.get("next_cursor")
    )
    has_more = bool(
        body.get("hasMore")
        or body.get("has_more")
        or pagination.get("hasMore")
        or pagination.get("has_more")
        or False
    )
    return InboxConversationsResult(
        conversations=conversations,
        next_cursor=str(next_cursor) if next_cursor is not None else None,
        has_more=has_more,
    )


def _map_inbox_messages(
    body: dict[str, Any],
    *,
    conversation_id: str,
    account_id: str,
) -> InboxMessagesResult:
    rows_raw = body.get("messages") or body.get("data") or body.get("items")
    rows = rows_raw if isinstance(rows_raw, list) else []
    messages: list[InboxMessage] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        msg_id = row.get("_id") or row.get("id") or row.get("messageId")
        sender: dict[str, Any] = {}
        sender_raw = row.get("sender")
        if isinstance(sender_raw, dict):
            sender = sender_raw
        direction_raw = str(
            row.get("direction") or row.get("type") or "incoming"
        ).lower()
        if direction_raw in {"outgoing", "outbound", "sent"}:
            direction = "outgoing"
        else:
            direction = "incoming"
        messages.append(
            InboxMessage(
                id=str(msg_id or ""),
                conversation_id=str(
                    row.get("conversationId")
                    or row.get("conversation_id")
                    or conversation_id
                ),
                account_id=str(
                    row.get("accountId") or row.get("account_id") or account_id
                ),
                direction=direction,
                text=str(
                    row.get("text") or row.get("message") or row.get("body") or ""
                ),
                sender_name=_str_or_none(
                    row.get("senderName")
                    or row.get("sender_name")
                    or sender.get("name")
                ),
                sender_handle=_str_or_none(
                    row.get("senderHandle")
                    or row.get("sender_handle")
                    or sender.get("username")
                    or sender.get("handle")
                ),
                created_at=_str_or_none(
                    row.get("timestamp")
                    or row.get("createdAt")
                    or row.get("created_at")
                ),
            )
        )
    pagination: dict[str, Any] = {}
    pagination_raw = body.get("pagination")
    if isinstance(pagination_raw, dict):
        pagination = pagination_raw
    next_cursor = (
        body.get("nextCursor")
        or body.get("next_cursor")
        or pagination.get("nextCursor")
        or pagination.get("next_cursor")
    )
    has_more = bool(
        body.get("hasMore")
        or body.get("has_more")
        or pagination.get("hasMore")
        or pagination.get("has_more")
        or False
    )
    return InboxMessagesResult(
        messages=messages,
        next_cursor=str(next_cursor) if next_cursor is not None else None,
        has_more=has_more,
    )


def _map_inbox_comment(
    row: dict[str, Any], *, post_id: str, account_id: str
) -> InboxComment:
    comment_id = row.get("_id") or row.get("id") or row.get("commentId")
    return InboxComment(
        id=str(comment_id or ""),
        post_id=str(row.get("postId") or row.get("post_id") or post_id),
        account_id=str(row.get("accountId") or row.get("account_id") or account_id),
        message=str(row.get("message") or row.get("text") or row.get("body") or ""),
        author_name=_str_or_none(row.get("authorName") or row.get("author_name")),
        author_handle=_str_or_none(
            row.get("authorHandle") or row.get("author_handle") or row.get("username")
        ),
        created_at=_str_or_none(row.get("createdAt") or row.get("created_at")),
        parent_comment_id=_str_or_none(
            row.get("parentCommentId") or row.get("parent_comment_id")
        ),
    )


def _normalize_inbox_send(
    status: int,
    payload: dict[str, Any] | list[Any] | None,
    headers: httpx.Headers,
) -> InboxSendResult:
    body = payload if isinstance(payload, dict) else {}
    replayed = _header_truthy(headers.get("Idempotent-Replayed"))

    if status == 409:
        return InboxSendResult(
            kind="in_flight",
            http_status=status,
            error_message=_error_message(body),
        )
    if status == 422:
        return InboxSendResult(
            kind="conflict",
            http_status=status,
            error_message=_error_message(body),
        )
    if status >= 500 or status == 0:
        return InboxSendResult(
            kind="ambiguous",
            http_status=status,
            error_message=_error_message(body) or f"upstream status {status}",
        )
    if status >= 400:
        return InboxSendResult(
            kind="failed",
            http_status=status,
            error_message=_error_message(body),
        )

    msg_id = (
        body.get("messageId")
        or body.get("message_id")
        or body.get("_id")
        or body.get("id")
    )
    nested = body.get("message") or body.get("data")
    if msg_id is None and isinstance(nested, dict):
        msg_id = nested.get("_id") or nested.get("id") or nested.get("messageId")

    return InboxSendResult(
        kind="replayed" if replayed else "sent",
        external_message_id=str(msg_id) if msg_id is not None else None,
        http_status=status,
        idempotent_replayed=replayed,
    )


def _header_truthy(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"true", "1", "yes"}


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

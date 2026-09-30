"""In-memory SocialProvider for tests and local development — zero network."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.integrations.errors import (
    AnalyticsCursorExpired,
    ProviderPaymentRequiredError,
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
    ValidatePostResult,
)

PublishScript = Literal[
    "created",
    "existing",
    "duplicate_conflict",
    "rate_limited",
    "rejected",
    "auth_failed",
    "payment_required",
    "non_definitive",
    "accepted",  # 201 with platforms still pending/publishing
]

AnalyticsDeltaScript = Literal[
    "non_empty",
    "has_more_chain",
    "empty_page",
    "addon_required",
    "unavailable_503",
    "cursor_expired_400",
]

InboxReplyScript = Literal[
    "sent",
    "replayed",
    "in_flight_409",
    "conflict_422",
    "ambiguous_5xx_after_accept",
]

DeleteAccountScript = Literal["ok", "not_found", "unavailable"]


class FakeSocialProvider:
    """Records every call; returns deterministic IDs and sensible defaults."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._profiles: dict[str, ProfileInfo] = {}
        self._accounts: dict[str, AccountInfo] = {}
        self._posts: dict[str, dict[str, Any]] = {}
        self._by_idempotency: dict[str, str] = {}
        self._profile_seq = 0
        self._account_seq = 0
        self._post_seq = 0
        self.publish_script: PublishScript = "created"
        self.publish_existing_post_id: str | None = None
        self.publish_error_category: str | None = None
        self.publish_retry_after: float | None = 60.0
        self.analytics_delta_script: AnalyticsDeltaScript = "empty_page"
        self._delta_page = 0
        self.inbox_reply_script: InboxReplyScript = "sent"
        self._send_call_count = 0
        self._inbox_by_idempotency: dict[str, InboxSendResult] = {}
        # Outbound messages "accepted" by the provider (incl. ambiguous 5xx) —
        # surfaced via get_post_comments for reconcile.
        self._listed_outbound: list[dict[str, Any]] = []
        # Fail the next N get_connect_url calls with a payment/capacity error.
        self.connect_url_failures_remaining: int = 0
        self.connect_url_fail_reason: str | None = "free_tier_exceeded"
        self.delete_account_script: DeleteAccountScript = "ok"

    def _record(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))

    def publish_call_count(self) -> int:
        return sum(1 for name, _ in self.calls if name == "publish")

    @property
    def send_call_count(self) -> int:
        return self._send_call_count

    async def verify_auth(self) -> AuthVerifyResult:
        self._record("verify_auth")
        return AuthVerifyResult(
            valid=True,
            user_id="fake_user",
            auth_type="api_key",
            scope="full",
        )

    async def list_profiles(self) -> list[ProfileInfo]:
        self._record("list_profiles")
        return list(self._profiles.values())

    async def create_profile(self, name: str, description: str | None = None) -> ProfileInfo:
        self._record("create_profile", name=name, description=description)
        import uuid as _uuid

        self._profile_seq += 1
        profile = ProfileInfo(
            id=f"prof_{self._profile_seq}_{_uuid.uuid4().hex[:8]}",
            name=name,
            description=description,
        )
        self._profiles[profile.id] = profile
        return profile

    async def get_connect_url(
        self,
        platform: str,
        profile_id: str,
        redirect_url: str,
        *,
        login_method: str | None = None,
    ) -> ConnectUrlResult:
        self._record(
            "get_connect_url",
            platform=platform,
            profile_id=profile_id,
            redirect_url=redirect_url,
            login_method=login_method,
        )
        if self.connect_url_failures_remaining > 0:
            self.connect_url_failures_remaining -= 1
            raise ProviderPaymentRequiredError(
                "fake connect payment required",
                reason=self.connect_url_fail_reason,
            )
        # Simulate a successful OAuth so subsequent list_accounts/sync find a row.
        existing = [
            a
            for a in self._accounts.values()
            if a.profile_id == profile_id and a.platform == platform
        ]
        if existing:
            account = existing[0]
        else:
            account = self.seed_account(platform=platform, profile_id=profile_id)
        # Point the browser at our callback with Zernio standard-mode success
        # params — never invent an unresolvable host like zernio.test.
        parts = urlsplit(redirect_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(
            {
                "connected": platform,
                "profileId": profile_id,
                "accountId": account.id,
                "username": account.username,
            }
        )
        auth_url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
        return ConnectUrlResult(
            auth_url=auth_url,
            provider_state=f"state_{platform}_{profile_id}",
        )

    async def list_accounts(
        self, *, profile_id: str | None = None, platform: str | None = None
    ) -> list[AccountInfo]:
        self._record("list_accounts", profile_id=profile_id, platform=platform)
        accounts = list(self._accounts.values())
        if profile_id is not None:
            accounts = [a for a in self._accounts.values() if a.profile_id == profile_id]
        if platform is not None:
            accounts = [a for a in accounts if a.platform == platform]
        return accounts

    async def get_accounts_health(self, *, profile_id: str | None = None) -> list[AccountHealth]:
        self._record("get_accounts_health", profile_id=profile_id)
        accounts = list(self._accounts.values())
        if profile_id is not None:
            accounts = [a for a in accounts if a.profile_id == profile_id]
        return [
            AccountHealth(
                account_id=a.id,
                status="healthy",
                can_post=True,
                can_fetch_analytics=True,
                token_valid=True,
                token_expires_at=None,
                needs_reconnect=False,
                issues=[],
            )
            for a in accounts
        ]

    async def delete_account(self, account_id: str) -> None:
        self._record("delete_account", account_id=account_id)
        script = self.delete_account_script
        if script == "not_found":
            # Idempotent — mirrors ZernioClient treating HTTP 404 as success.
            return
        if script == "unavailable":
            raise ProviderUnavailableError("fake delete unavailable")
        self._accounts.pop(account_id, None)

    async def validate_post(self, payload: dict[str, Any]) -> ValidatePostResult:
        self._record("validate_post", payload=payload)
        return ValidatePostResult(valid=True, errors=[], warnings=[])

    async def publish(self, request: PublishRequest) -> PublishResult:
        self._record(
            "publish",
            idempotency_key=request.idempotency_key,
            content=request.content,
            platforms=request.platforms,
            metadata=request.metadata,
        )
        # Idempotent replay within the fake's window.
        prior = self._by_idempotency.get(request.idempotency_key)
        if prior is not None and prior in self._posts:
            stored = self._posts[prior]
            return PublishResult(
                kind="existing",
                zernio_post_id=prior,
                platforms=_platforms_from_stored(stored),
                http_status=200,
                raw_response={"existingPost": stored},
            )

        script = self.publish_script
        if script == "rate_limited":
            return PublishResult(
                kind="rate_limited",
                http_status=429,
                retry_after=self.publish_retry_after,
                error_message="Rate limit exceeded",
            )
        if script == "auth_failed":
            return PublishResult(
                kind="auth_failed",
                http_status=401,
                error_message="Invalid API key",
            )
        if script == "payment_required":
            return PublishResult(
                kind="payment_required",
                http_status=402,
                error_message="Payment required",
            )
        if script == "rejected":
            return PublishResult(
                kind="rejected",
                http_status=400,
                error_category=self.publish_error_category or "platform_rejected",
                error_message="Rejected by platform",
            )
        if script == "non_definitive":
            return PublishResult(
                kind="non_definitive",
                http_status=503,
                error_message="upstream unavailable",
            )
        if script == "duplicate_conflict":
            return PublishResult(
                kind="duplicate_conflict",
                existing_post_id=self.publish_existing_post_id or "z_other",
                http_status=409,
                error_message="Duplicate content",
            )

        self._post_seq += 1
        import uuid as _uuid

        zernio_id = f"zpost_{self._post_seq}_{_uuid.uuid4().hex[:10]}"
        platform_status = "publishing" if script == "accepted" else "published"
        account_id = ""
        platform = "instagram"
        if request.platforms:
            first = request.platforms[0]
            account_id = str(first.get("accountId") or "")
            platform = str(first.get("platform") or "instagram")
        stored = {
            "_id": zernio_id,
            "platforms": [
                {
                    "platform": platform,
                    "accountId": account_id,
                    "status": platform_status,
                    "platformPostId": f"ext_{self._post_seq}"
                    if platform_status == "published"
                    else None,
                    "platformPostUrl": (
                        f"https://instagram.test/p/{self._post_seq}"
                        if platform_status == "published"
                        else None
                    ),
                }
            ],
            "metadata": dict(request.metadata),
        }
        self._posts[zernio_id] = stored
        self._by_idempotency[request.idempotency_key] = zernio_id
        return PublishResult(
            kind="created",
            zernio_post_id=zernio_id,
            platforms=_platforms_from_stored(stored),
            http_status=201,
            raw_response=stored,
        )

    async def get_post(self, zernio_post_id: str) -> PublishResult:
        self._record("get_post", zernio_post_id=zernio_post_id)
        stored = self._posts.get(zernio_post_id)
        if stored is None:
            return PublishResult(
                kind="rejected",
                http_status=404,
                error_message="not found",
            )
        # Reconciliation resolves pending → published.
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
        return PublishResult(
            kind="existing",
            zernio_post_id=zernio_post_id,
            platforms=_platforms_from_stored(stored),
            http_status=200,
            raw_response=stored,
        )

    async def get_analytics_delta(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> AnalyticsDeltaResult:
        self._record("get_analytics_delta", cursor=cursor, limit=limit)
        script = self.analytics_delta_script
        if script == "addon_required":
            raise ProviderPaymentRequiredError("analytics addon required")
        if script == "unavailable_503":
            raise ProviderUnavailableError("analytics unavailable", retry_after=5.0)
        if script == "cursor_expired_400":
            raise AnalyticsCursorExpired("cursor expired")
        if script == "empty_page":
            return AnalyticsDeltaResult(
                data=[],
                next_cursor=cursor or "cursor_bootstrap",
                has_more=False,
            )
        if script == "has_more_chain":
            self._delta_page += 1
            if self._delta_page == 1:
                return AnalyticsDeltaResult(
                    data=[_sample_delta_entry("zp_page1")],
                    next_cursor="cursor_page2",
                    has_more=True,
                )
            return AnalyticsDeltaResult(
                data=[_sample_delta_entry("zp_page2")],
                next_cursor="cursor_done",
                has_more=False,
            )
        # non_empty
        return AnalyticsDeltaResult(
            data=[_sample_delta_entry("zp_delta_1")],
            next_cursor="cursor_after_1",
            has_more=False,
        )

    async def get_analytics_baseline(self, *, limit: int = 50) -> AnalyticsBaselineResult:
        self._record("get_analytics_baseline", limit=limit)
        if self.analytics_delta_script == "addon_required":
            raise ProviderPaymentRequiredError("analytics addon required")
        return AnalyticsBaselineResult(
            has_analytics_access=True,
            accounts=[
                AnalyticsBaselineAccount(
                    account_id="acc_fake_1",
                    platform="instagram",
                    follower_count=1200,
                )
            ],
            posts=[
                AnalyticsBaselinePost(
                    post_id="zp_base_1",
                    account_id="acc_fake_1",
                    platform="instagram",
                    published_at="2024-11-01T10:00:05Z",
                    metrics={"impressions": 100, "likes": 10},
                )
            ],
        )

    async def list_inbox_conversations(
        self,
        *,
        account_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> InboxConversationsResult:
        self._record(
            "list_inbox_conversations",
            account_id=account_id,
            limit=limit,
            cursor=cursor,
        )
        return InboxConversationsResult(
            conversations=[
                InboxConversation(
                    id="conv_fake_1",
                    account_id=account_id or "acc_fake_1",
                    platform="instagram",
                    status="open",
                    participant_name="Ada",
                    participant_handle="@ada",
                    last_message_at="2024-11-02T08:00:00Z",
                    last_message_preview="Hello there",
                    unread_count=1,
                )
            ],
            next_cursor=None,
            has_more=False,
        )

    async def list_inbox_messages(
        self,
        *,
        conversation_id: str,
        account_id: str,
        limit: int = 100,
        cursor: str | None = None,
        sort_order: str = "asc",
    ) -> InboxMessagesResult:
        self._record(
            "list_inbox_messages",
            conversation_id=conversation_id,
            account_id=account_id,
            limit=limit,
            cursor=cursor,
            sort_order=sort_order,
        )
        return InboxMessagesResult(
            messages=[
                InboxMessage(
                    id="msg_fake_1",
                    conversation_id=conversation_id,
                    account_id=account_id,
                    direction="incoming",
                    text="Hello there",
                    sender_name="Ada",
                    sender_handle="@ada",
                    created_at="2024-11-02T08:00:00Z",
                ),
                InboxMessage(
                    id="msg_fake_2",
                    conversation_id=conversation_id,
                    account_id=account_id,
                    direction="outgoing",
                    text="Thanks for reaching out!",
                    sender_name="Brand",
                    created_at="2024-11-02T08:05:00Z",
                ),
            ],
            next_cursor=None,
            has_more=False,
        )

    async def get_post_comments(self, *, post_id: str, account_id: str) -> list[InboxComment]:
        self._record("get_post_comments", post_id=post_id, account_id=account_id)
        base = [
            InboxComment(
                id="cmt_fake_1",
                post_id=post_id,
                account_id=account_id,
                message="Nice post!",
                author_name="Ada",
                author_handle="@ada",
                created_at="2024-11-02T09:00:00Z",
            )
        ]
        extras: list[InboxComment] = []
        for row in self._listed_outbound:
            thread = row.get("post_id") or row.get("conversation_id")
            if thread != post_id:
                continue
            extras.append(
                InboxComment(
                    id=str(row["id"]),
                    post_id=post_id,
                    account_id=account_id,
                    message=str(row["message"]),
                    author_name="Brand",
                    created_at=str(row.get("created_at") or "2024-11-02T10:00:00Z"),
                )
            )
        # When listing for a DM conversation id, skip the canned comment.
        if any(r.get("conversation_id") == post_id for r in self._listed_outbound) and not any(
            r.get("post_id") == post_id for r in self._listed_outbound
        ):
            return extras
        return base + extras

    async def send_inbox_message(
        self,
        *,
        conversation_id: str,
        account_id: str,
        message: str,
        idempotency_key: str,
    ) -> InboxSendResult:
        self._record(
            "send_inbox_message",
            conversation_id=conversation_id,
            account_id=account_id,
            message=message,
            idempotency_key=idempotency_key,
        )
        result = self._scripted_inbox_send(idempotency_key)
        self._maybe_list_outbound(
            result,
            conversation_id=conversation_id,
            message=message,
        )
        return result

    async def reply_to_post_comment(
        self,
        *,
        post_id: str,
        account_id: str,
        message: str,
        idempotency_key: str,
        comment_id: str | None = None,
    ) -> InboxSendResult:
        self._record(
            "reply_to_post_comment",
            post_id=post_id,
            account_id=account_id,
            message=message,
            idempotency_key=idempotency_key,
            comment_id=comment_id,
        )
        result = self._scripted_inbox_send(idempotency_key)
        self._maybe_list_outbound(
            result,
            post_id=post_id,
            message=message,
        )
        return result

    def _maybe_list_outbound(
        self,
        result: InboxSendResult,
        *,
        message: str,
        conversation_id: str | None = None,
        post_id: str | None = None,
    ) -> None:
        script = self.inbox_reply_script
        if result.kind in ("sent", "replayed") or script == "ambiguous_5xx_after_accept":
            ext_id = result.external_message_id or f"msg_ambig_{self._send_call_count}"
            self._listed_outbound.append(
                {
                    "id": ext_id,
                    "message": message,
                    "conversation_id": conversation_id,
                    "post_id": post_id,
                    "created_at": "2024-11-02T10:00:00Z",
                }
            )

    def _scripted_inbox_send(self, idempotency_key: str) -> InboxSendResult:
        prior = self._inbox_by_idempotency.get(idempotency_key)
        if prior is not None and prior.kind in ("sent", "replayed"):
            return InboxSendResult(
                kind="replayed",
                external_message_id=prior.external_message_id,
                http_status=200,
                idempotent_replayed=True,
            )

        self._send_call_count += 1
        script = self.inbox_reply_script
        if script == "in_flight_409":
            return InboxSendResult(kind="in_flight", http_status=409)
        if script == "conflict_422":
            return InboxSendResult(
                kind="conflict",
                http_status=422,
                error_message="idempotency key conflict",
            )
        if script == "ambiguous_5xx_after_accept":
            # First (and every) call looks accepted to the network layer as 5xx
            # after the provider may have applied — exposes double-send on blind retry.
            return InboxSendResult(
                kind="ambiguous",
                http_status=503,
                error_message="upstream 5xx after accept",
            )
        if script == "replayed":
            result = InboxSendResult(
                kind="replayed",
                external_message_id="msg_replayed_1",
                http_status=200,
                idempotent_replayed=True,
            )
            self._inbox_by_idempotency[idempotency_key] = result
            return result

        result = InboxSendResult(
            kind="sent",
            external_message_id=f"msg_sent_{self._send_call_count}",
            http_status=201,
            idempotent_replayed=False,
        )
        self._inbox_by_idempotency[idempotency_key] = result
        return result

    def seed_account(
        self,
        *,
        platform: str = "instagram",
        profile_id: str = "prof_1",
        username: str = "brand",
    ) -> AccountInfo:
        """Test helper — add a connected account without going through OAuth."""
        import uuid as _uuid

        self._account_seq += 1
        account = AccountInfo(
            id=f"acct_{self._account_seq}_{_uuid.uuid4().hex[:8]}",
            platform=platform,
            profile_id=profile_id,
            username=username,
            display_name=username,
        )
        self._accounts[account.id] = account
        return account

    def seed_post(
        self,
        *,
        zernio_post_id: str,
        account_id: str,
        platform: str = "instagram",
        status: str = "published",
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Test helper — pre-seed a Zernio post for crash-recovery paths."""
        stored = {
            "_id": zernio_post_id,
            "platforms": [
                {
                    "platform": platform,
                    "accountId": account_id,
                    "status": status,
                    "platformPostId": "ext_seeded",
                    "platformPostUrl": "https://instagram.test/p/seeded",
                }
            ],
            "metadata": metadata or {},
        }
        self._posts[zernio_post_id] = stored
        if idempotency_key is not None:
            self._by_idempotency[idempotency_key] = zernio_post_id


def _sample_delta_entry(post_id: str) -> AnalyticsDeltaEntry:
    return AnalyticsDeltaEntry(
        post_id=post_id,
        account_id="acc_fake_1",
        profile_id="prof_fake_1",
        platform="instagram",
        platform_post_id="ext_1",
        published_at="2024-11-01T10:00:05Z",
        synced_at="2024-11-02T08:30:00Z",
        is_deleted=False,
        metrics={"impressions": 15420, "likes": 342},
    )


def _platforms_from_stored(stored: dict[str, Any]) -> list[PlatformOutcome]:
    raw = stored.get("platforms")
    if not isinstance(raw, list):
        return []
    out: list[PlatformOutcome] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        out.append(
            PlatformOutcome(
                platform=str(row.get("platform") or ""),
                account_id=str(row.get("accountId") or ""),
                status=str(row.get("status") or "pending"),
                platform_post_id=(
                    str(row["platformPostId"]) if row.get("platformPostId") is not None else None
                ),
                platform_post_url=(
                    str(row["platformPostUrl"]) if row.get("platformPostUrl") is not None else None
                ),
                error_category=(
                    str(row["errorCategory"]) if row.get("errorCategory") is not None else None
                ),
                error_message=(
                    str(row["errorMessage"]) if row.get("errorMessage") is not None else None
                ),
            )
        )
    return out

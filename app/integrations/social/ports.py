"""SocialProvider port — connect/health + publish + analytics/inbox (Phase 12)."""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ProfileInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    description: str | None = None


class ConnectUrlResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    auth_url: str
    provider_state: str


class AccountInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    platform: str
    profile_id: str
    username: str
    display_name: str | None = None
    profile_picture: str | None = None
    is_active: bool = True
    needs_reconnection: bool = False


class AccountHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    status: str  # healthy | warning | error
    can_post: bool
    can_fetch_analytics: bool
    token_valid: bool
    token_expires_at: str | None = None
    needs_reconnect: bool = False
    issues: list[str] = Field(default_factory=list)


class AuthVerifyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    user_id: str | None = None
    auth_type: str | None = None
    scope: str | None = None


class ValidatePostResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RateLimitInfo(BaseModel):
    """Parsed X-RateLimit-* headers from a Zernio response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    limit: int | None = None
    remaining: int | None = None
    reset_at_unix: int | None = None


PublishOutcomeKind = Literal[
    "created",
    "existing",
    "duplicate_conflict",
    "rate_limited",
    "rejected",
    "auth_failed",
    "payment_required",
    "non_definitive",
]


class PlatformOutcome(BaseModel):
    """Normalized per-platform slice of a Zernio post response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    account_id: str
    status: str  # pending | publishing | published | failed
    platform_post_id: str | None = None
    platform_post_url: str | None = None
    error_category: str | None = None
    error_message: str | None = None


class PublishRequest(BaseModel):
    """Outbound publish payload — assembled by the publishing service."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    platforms: list[dict[str, Any]]
    media_items: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    title: str | None = None


class PublishResult(BaseModel):
    """Normalized publish/get_post outcome — never a raw Zernio body.

    ``apply_publish_result`` consumes this shape only (architecture/10 §6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PublishOutcomeKind
    zernio_post_id: str | None = None
    existing_post_id: str | None = None
    platforms: list[PlatformOutcome] = Field(default_factory=list)
    error_category: str | None = None
    error_message: str | None = None
    retry_after: float | None = None
    http_status: int | None = None
    raw_response: dict[str, Any] | None = None


class AnalyticsDeltaEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    post_id: str
    account_id: str
    profile_id: str
    platform: str
    platform_post_id: str | None = None
    published_at: str | None = None
    synced_at: str | None = None
    is_deleted: bool = False
    metrics: dict[str, Any] = Field(default_factory=dict)


class AnalyticsDeltaResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data: list[AnalyticsDeltaEntry] = Field(default_factory=list)
    next_cursor: str
    has_more: bool = False


class AnalyticsBaselineAccount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    platform: str
    follower_count: int | None = None


class AnalyticsBaselinePost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    post_id: str
    account_id: str
    platform: str
    published_at: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class AnalyticsBaselineResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    has_analytics_access: bool
    accounts: list[AnalyticsBaselineAccount] = Field(default_factory=list)
    posts: list[AnalyticsBaselinePost] = Field(default_factory=list)


class InboxConversation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    account_id: str
    platform: str
    status: str | None = None
    participant_name: str | None = None
    participant_handle: str | None = None
    last_message_at: str | None = None
    last_message_preview: str | None = None
    unread_count: int | None = None


class InboxConversationsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conversations: list[InboxConversation] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class InboxMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    conversation_id: str
    account_id: str
    direction: str  # incoming | outgoing
    text: str
    sender_name: str | None = None
    sender_handle: str | None = None
    created_at: str | None = None


class InboxMessagesResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: list[InboxMessage] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class InboxComment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    post_id: str
    account_id: str
    message: str
    author_name: str | None = None
    author_handle: str | None = None
    created_at: str | None = None
    parent_comment_id: str | None = None


InboxSendKind = Literal[
    "sent",
    "replayed",
    "in_flight",
    "conflict",
    "failed",
    "ambiguous",
]


class InboxSendResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: InboxSendKind
    external_message_id: str | None = None
    http_status: int | None = None
    error_message: str | None = None
    idempotent_replayed: bool = False


@runtime_checkable
class SocialProvider(Protocol):
    """Zernio (or fake) adapter — connect/health + publish + analytics/inbox."""

    async def verify_auth(self) -> AuthVerifyResult: ...

    async def list_profiles(self) -> list[ProfileInfo]: ...

    async def create_profile(
        self, name: str, description: str | None = None
    ) -> ProfileInfo: ...

    async def get_connect_url(
        self,
        platform: str,
        profile_id: str,
        redirect_url: str,
        *,
        login_method: str | None = None,
    ) -> ConnectUrlResult: ...

    async def list_accounts(
        self, *, profile_id: str | None = None, platform: str | None = None
    ) -> list[AccountInfo]: ...

    async def get_accounts_health(
        self, *, profile_id: str | None = None
    ) -> list[AccountHealth]: ...

    async def delete_account(self, account_id: str) -> None: ...

    async def validate_post(self, payload: dict[str, Any]) -> ValidatePostResult: ...

    async def publish(self, request: PublishRequest) -> PublishResult: ...

    async def get_post(self, zernio_post_id: str) -> PublishResult: ...

    async def get_analytics_delta(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> AnalyticsDeltaResult: ...

    async def get_analytics_baseline(
        self, *, limit: int = 50
    ) -> AnalyticsBaselineResult: ...

    async def list_inbox_conversations(
        self,
        *,
        account_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> InboxConversationsResult: ...

    async def list_inbox_messages(
        self,
        *,
        conversation_id: str,
        account_id: str,
        limit: int = 100,
        cursor: str | None = None,
        sort_order: str = "asc",
    ) -> InboxMessagesResult: ...

    async def get_post_comments(
        self, *, post_id: str, account_id: str
    ) -> list[InboxComment]: ...

    async def send_inbox_message(
        self,
        *,
        conversation_id: str,
        account_id: str,
        message: str,
        idempotency_key: str,
    ) -> InboxSendResult: ...

    async def reply_to_post_comment(
        self,
        *,
        post_id: str,
        account_id: str,
        message: str,
        idempotency_key: str,
        comment_id: str | None = None,
    ) -> InboxSendResult: ...

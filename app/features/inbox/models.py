"""conversations / conversation_messages / saved_replies — architecture/02 §16."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    BrandScopedMixin,
    CreatedAtMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

_PLATFORM_VALUES = ("instagram", "facebook", "tiktok", "snapchat", "whatsapp")
_KIND_VALUES = ("dm", "comment")
_SENTIMENT_VALUES = ("positive", "neutral", "negative")
_STATUS_VALUES = ("open", "resolved")
_DIRECTION_VALUES = ("inbound", "outbound")
_LANG_VALUES = ("ar", "en")
_DELIVERY_STATUS_VALUES = ("received", "pending", "sent", "failed")
_SYNC_STATUS_VALUES = ("ok", "error", "gated")


class Conversation(Base, UUIDPrimaryKeyMixin, TimestampMixin, BrandScopedMixin):
    """Unified DM/comment thread — brand-scoped, listed org-wide."""

    __tablename__ = "conversations"

    social_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("social_accounts.id"), nullable=False
    )
    platform: Mapped[str]
    kind: Mapped[str]
    external_thread_id: Mapped[str]
    participant_name: Mapped[str]
    participant_handle: Mapped[str | None] = mapped_column(default=None)
    participant_avatar_url: Mapped[str | None] = mapped_column(default=None)
    participant_external_id: Mapped[str | None] = mapped_column(default=None)
    post_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), default=None
    )
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]")
    )
    sentiment: Mapped[str] = mapped_column(server_default="neutral")
    sentiment_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id"), default=None
    )
    status: Mapped[str] = mapped_column(server_default="open")
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    escalated: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    escalated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    suggested_reply_ar: Mapped[str] = mapped_column(server_default="")
    suggested_reply_en: Mapped[str] = mapped_column(server_default="")
    suggestion_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id"), default=None
    )
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_inbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    message_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(
            "platform IN (" + ", ".join(f"'{v}'" for v in _PLATFORM_VALUES) + ")",
            name="platform_valid",
        ),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{v}'" for v in _KIND_VALUES) + ")",
            name="kind_valid",
        ),
        CheckConstraint(
            "sentiment IN (" + ", ".join(f"'{v}'" for v in _SENTIMENT_VALUES) + ")",
            name="sentiment_valid",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _STATUS_VALUES) + ")",
            name="status_valid",
        ),
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_conversations_brand_org",
            ondelete="CASCADE",
        ),
        Index(
            "ux_conv_external",
            "social_account_id",
            "external_thread_id",
            unique=True,
        ),
        Index(
            "ix_conv_org_last",
            "organization_id",
            text("last_message_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_conv_open",
            "organization_id",
            text("last_message_at DESC"),
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_conv_tags", "tags", postgresql_using="gin"),
        Index(
            "ix_conv_assignee",
            "assignee_user_id",
            postgresql_where=text("status = 'open'"),
        ),
    )


class ConversationMessage(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One message in a conversation thread — oldest-first index."""

    __tablename__ = "conversation_messages"

    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str]
    body: Mapped[str]
    lang: Mapped[str]
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    author_name: Mapped[str]
    external_message_id: Mapped[str | None] = mapped_column(default=None)
    idempotency_key: Mapped[str | None] = mapped_column(default=None)
    delivery_status: Mapped[str]
    error_code: Mapped[str | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    __table_args__ = (
        CheckConstraint(
            "direction IN (" + ", ".join(f"'{v}'" for v in _DIRECTION_VALUES) + ")",
            name="direction_valid",
        ),
        CheckConstraint(
            "lang IN (" + ", ".join(f"'{v}'" for v in _LANG_VALUES) + ")",
            name="lang_valid",
        ),
        CheckConstraint(
            "delivery_status IN ("
            + ", ".join(f"'{v}'" for v in _DELIVERY_STATUS_VALUES)
            + ")",
            name="delivery_status_valid",
        ),
        # Ascending — chat thread order, not newest-first (architecture/02 §16).
        Index("ix_conv_messages", "conversation_id", "created_at", "id"),
        Index(
            "ux_conv_message_external",
            "conversation_id",
            "external_message_id",
            unique=True,
            postgresql_where=text("external_message_id IS NOT NULL"),
        ),
        Index(
            "ux_conv_message_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )


class SavedReply(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, SoftDeleteMixin):
    """Org-scoped canned reply template."""

    __tablename__ = "saved_replies"

    title: Mapped[str]
    body_ar: Mapped[str]
    body_en: Mapped[str]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    usage_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    __table_args__ = (
        Index(
            "ux_saved_reply_title",
            "organization_id",
            text("lower(title)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class InboxSyncState(Base):
    """Per-social-account inbox backfill cursor / last sync outcome."""

    __tablename__ = "inbox_sync_state"

    social_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("social_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    dm_cursor: Mapped[str | None] = mapped_column(default=None)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_sync_status: Mapped[str] = mapped_column(server_default="ok")
    last_error: Mapped[str | None] = mapped_column(default=None)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, server_default=text("0")
    )

    __table_args__ = (
        CheckConstraint(
            "last_sync_status IN ("
            + ", ".join(f"'{v}'" for v in _SYNC_STATUS_VALUES)
            + ")",
            name="last_sync_status_valid",
        ),
    )

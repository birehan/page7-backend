from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TenantMixin, UUIDPrimaryKeyMixin

# Original 10-value set (architecture/02 §10) plus `channel_disconnected`
# (Phase 9) and `ai_credits_warning` (Phase 13).
_NOTIFICATION_TYPES = (
    "approval_requested, post_approved, post_rejected, post_published, "
    "post_failed, changes_requested, channel_expiring, channel_disconnected, "
    "weekly_insight, plan_generated, mention, ai_credits_warning"
).replace(", ", "', '")


class Notification(Base, UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin):
    """architecture/02 §10. Fan-out to recipients happens at insert time
    (features/notifications/service.py) — org members filtered by role and by
    `org_settings.notification_prefs`. No caller produces a row this phase; the
    feed is correctly, deliberately empty until Phase 6's first `post.submit`.
    """

    __tablename__ = "notifications"

    brand_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    type: Mapped[str]
    message_key: Mapped[str]
    params: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    target_href: Mapped[str]
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    actor_ref: Mapped[str | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(f"type IN ('{_NOTIFICATION_TYPES}')", name="type_valid"),
        Index(
            "ix_notifications_org_created",
            "organization_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )


class NotificationRecipient(Base):
    """architecture/02 §10. The contract's `readAt` is the caller's own row
    here; mark-all-read only ever touches the caller's own rows.
    """

    __tablename__ = "notification_recipients"

    notification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    read_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        Index(
            "ix_notif_unread",
            "user_id",
            text("notification_id DESC"),
            postgresql_where=text("read_at IS NULL"),
        ),
        Index("ix_notif_feed", "user_id", text("notification_id DESC")),
    )

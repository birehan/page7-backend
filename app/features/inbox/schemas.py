"""Wire schemas for inbox endpoints — camelCase via CamelModel."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.pagination import Page
from app.core.schema import CamelModel

ConversationSentiment = Literal["positive", "neutral", "negative"]
ConversationStatus = Literal["open", "resolved"]
MessageDirection = Literal["inbound", "outbound"]
MessageLang = Literal["ar", "en"]


class ConversationParticipantOut(CamelModel):
    name: str
    handle: str | None = None
    avatar_url: str | None = None


class ConversationMessageOut(CamelModel):
    id: uuid.UUID
    direction: MessageDirection
    body: str
    author_name: str
    author_id: uuid.UUID | None = None
    created_at: datetime
    lang: MessageLang


class ConversationOut(CamelModel):
    id: uuid.UUID
    platform: str
    participant: ConversationParticipantOut
    messages: list[ConversationMessageOut]
    assignee_id: uuid.UUID | None = None
    tags: list[str]
    sentiment: ConversationSentiment
    status: ConversationStatus
    escalated: bool
    post_id: uuid.UUID | None = None
    suggested_reply_ar: str
    suggested_reply_en: str
    last_message_at: datetime
    created_at: datetime


ConversationPage = Page[ConversationOut]
ConversationMessagePage = Page[ConversationMessageOut]


class SendReplyBody(CamelModel):
    body: str = Field(min_length=1)


class AiDraftOut(CamelModel):
    suggested_reply_ar: str
    suggested_reply_en: str


class AssignConversationBody(CamelModel):
    assignee_id: uuid.UUID | None = None


class SetConversationTagsBody(CamelModel):
    tags: list[str]


class SavedReplyOut(CamelModel):
    id: uuid.UUID
    title: str
    body_ar: str
    body_en: str
    created_at: datetime


class CreateSavedReplyBody(CamelModel):
    title: str = Field(min_length=1)
    body_ar: str = Field(min_length=1)
    body_en: str = Field(min_length=1)


class InboxSyncBody(CamelModel):
    brand_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    skip_classify: bool = True
    conversation_limit: int = Field(default=25, ge=1, le=100)
    mode: Literal["initial", "more"] = "initial"


class InboxSyncEnqueueOut(CamelModel):
    enqueued: int
    account_ids: list[uuid.UUID]


class InboxSyncAccountStatusOut(CamelModel):
    account_id: uuid.UUID
    brand_id: uuid.UUID
    platform: str
    last_synced_at: datetime | None = None
    last_sync_status: str
    last_error: str | None = None
    has_more: bool = False


class InboxSyncStatusOut(CamelModel):
    accounts: list[InboxSyncAccountStatusOut]

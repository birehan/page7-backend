"""Inbox feature exceptions."""

from __future__ import annotations


class InboxError(Exception):
    """Base for inbox domain errors."""


class ConversationNotFound(InboxError):
    def __init__(self, message: str = "conversation not found") -> None:
        self.message = message
        super().__init__(message)


class MessageNotFound(InboxError):
    def __init__(self, message: str = "message not found") -> None:
        self.message = message
        super().__init__(message)


class AccountNotFound(InboxError):
    def __init__(self, message: str = "social account not found") -> None:
        self.message = message
        super().__init__(message)


class ReplyConflict(InboxError):
    def __init__(self, message: str = "reply idempotency conflict") -> None:
        self.message = message
        super().__init__(message)

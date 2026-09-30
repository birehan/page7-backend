"""Domain exceptions for social accounts / Zernio (Phase 9)."""

from __future__ import annotations

from app.integrations.errors import CredentialPoolExhausted

__all__ = [
    "AccountDisconnected",
    "AccountTokenExpired",
    "CredentialDisabled",
    "CredentialPoolExhausted",
]


class AccountDisconnected(Exception):
    """Account is disconnected and cannot be used for provider operations."""


class AccountTokenExpired(Exception):
    """Account token is expired / needs reconnect."""


class CredentialDisabled(Exception):
    """Pinned credential is disabled (401/402) — account-level operations blocked."""

"""Wire schemas for channels / Zernio (Phase 9)."""

from __future__ import annotations

import uuid
from typing import Literal

from app.core.schema import CamelModel

Platform = Literal["instagram", "facebook", "tiktok", "snapchat", "whatsapp"]
ChannelStatus = Literal["connected", "expiring", "expired", "disconnected"]


class SocialAccountOut(CamelModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    brand_id: uuid.UUID
    platform: Platform
    status: ChannelStatus
    handle: str
    token_expires_at: str | None = None
    connected_at: str


class ChannelOAuthUrlOut(CamelModel):
    authorize_url: str
    state: str


class ChannelOAuthUrlBody(CamelModel):
    """Optional post-OAuth frontend path. Allowlisted server-side."""

    return_path: str | None = None


class PlatformCapability(CamelModel):
    platform: Platform
    connectable: bool
    publishable: bool
    notes: str | None = None


class CapabilityMatrixOut(CamelModel):
    platforms: list[PlatformCapability]

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field

from app.core.schema import CamelModel


class WaitlistJoinIn(CamelModel):
    email: EmailStr
    business_name: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    locale: str | None = Field(default=None, max_length=5)
    source: str | None = Field(default=None, max_length=60)


class WaitlistJoinOut(CamelModel):
    id: uuid.UUID
    email: EmailStr
    already_joined: bool
    created_at: datetime

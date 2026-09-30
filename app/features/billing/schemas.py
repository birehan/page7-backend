"""Wire schemas for billing — camelCase via CamelModel.

Plan codes on the wire are Title Case (Starter/Growth/Agency); the DB stores
lowercase (starter/growth/agency). Mapping lives only here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.core.schema import CamelModel

WirePlan = Literal["Starter", "Growth", "Agency"]
WireSubscriptionStatus = Literal["active", "trialing", "past_due"]
WireInvoiceStatus = Literal["paid", "pending", "failed"]

PLAN_TO_DB: dict[WirePlan, str] = {
    "Starter": "starter",
    "Growth": "growth",
    "Agency": "agency",
}
DB_TO_PLAN: dict[str, WirePlan] = {v: k for k, v in PLAN_TO_DB.items()}


def plan_to_wire(code: str) -> WirePlan:
    try:
        return DB_TO_PLAN[code]
    except KeyError as exc:
        raise ValueError(f"unknown plan code: {code}") from exc


def plan_to_db(plan: WirePlan) -> str:
    return PLAN_TO_DB[plan]


class SubscriptionOut(CamelModel):
    plan: WirePlan
    price_monthly: float
    renews_at: datetime
    status: WireSubscriptionStatus


class InvoiceOut(CamelModel):
    id: uuid.UUID
    number: str
    amount: float
    vat_amount: float
    total: float
    currency: Literal["SAR"] = "SAR"
    status: WireInvoiceStatus
    issued_at: datetime


class UpdatePlanBody(CamelModel):
    plan: WirePlan


class AiUsageOut(CamelModel):
    used: int = Field(ge=0)


def money_to_float(value: Decimal) -> float:
    """Wire money fields are JSON numbers; DB stores numeric."""
    return float(value)

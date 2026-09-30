from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, MetaData, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Named constraints by convention (ADR-0009 adjacent): Alembic autogenerate then
# produces stable, predictable migration operations instead of database-assigned
# default names that differ across environments.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # SQLAlchemy's own defaults for these two Python types (String / naive
    # DateTime) silently disagree with this project's conventions — every text
    # column is `text` not `varchar(n)`, and every instant is timezone-aware
    # (architecture/02's UTC-instant discipline). Fixed here, once, so a bare
    # `Mapped[str]` / `Mapped[datetime]` is correct by default everywhere instead
    # of every model needing to remember an explicit override.
    type_annotation_map = {
        str: Text,
        datetime: DateTime(timezone=True),
    }


class UUIDPrimaryKeyMixin:
    """`uuid PRIMARY KEY DEFAULT uuidv7()` — every API-visible table (ADR-0009).
    The named high-churn exceptions (`jobs`, `webhook_events`, `metric_snapshots`)
    use a plain bigint identity column instead and do not use this mixin.
    """

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.uuidv7())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class CreatedAtMixin:
    """For tables that record when a row was created but never update it after
    insert (sessions, tokens, append-only logs) — `TimestampMixin` would add an
    `updated_at` column these tables don't have (architecture/02's shorthand).
    """

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TenantMixin:
    """`organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE
    CASCADE` — architecture/02's `T` shorthand. Every tenant-scoped table carries
    this so it can be filtered on `organization_id` alone, no join required.
    """

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )


class BrandScopedMixin:
    """architecture/02's `B` shorthand: tenant + brand, with integrity enforced
    by a composite FK `(brand_id, organization_id) → brands(id, organization_id)`
    on each concrete table's `__table_args__` (SQLAlchemy cannot express a
    composite FK cleanly on a mixin column alone).

    `organization_id` deliberately has no direct FK to `organizations` — the
    composite brand FK already guarantees the org exists via `brands`. Declaring
    both would diverge from the migration and fail `alembic check`.
    """

    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    brand_id: Mapped[uuid.UUID] = mapped_column(nullable=False)


class SoftDeleteMixin:
    """`deleted_at`/`deleted_by` — architecture/02's `sd` shorthand. `deleted_by`
    is `ON DELETE SET NULL`: the deleting user may later be anonymized without
    breaking this row's history.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(default=None)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

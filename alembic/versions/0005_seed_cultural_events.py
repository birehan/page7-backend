"""Seed cultural_events catalog (Phase 4 data migration).

Seed data migration. Downgrade deletes exactly the slugs this upgrade inserts
so CI round-trips; once Phase 6 adds posts.cultural_event_id FKs into these
rows, downgrade must become a raise.

Revision ID: 0005_seed_cultural_events
Revises: 0004_brands_strategy_cultural
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_seed_cultural_events"
down_revision: str | None = "0004_brands_strategy_cultural"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Import inside upgrade so Alembic's offline mode doesn't need the app
    # package's full import graph just to print SQL.
    from app.features.cultural_events.catalog import build_events

    events = build_events()
    if not events:
        return

    conn = op.get_bind()
    for event in events:
        conn.execute(
            sa.text(
                """
                INSERT INTO cultural_events (
                    slug, name, name_ar, start_date, end_date,
                    kind, hijri_year, source, enabled_by_default
                ) VALUES (
                    :slug, :name, :name_ar, :start_date, :end_date,
                    :kind, :hijri_year, :source, :enabled_by_default
                )
                ON CONFLICT (slug) DO NOTHING
                """
            ),
            {
                "slug": event.slug,
                "name": event.name,
                "name_ar": event.name_ar,
                "start_date": event.start_date,
                "end_date": event.end_date,
                "kind": event.kind,
                "hijri_year": event.hijri_year,
                "source": event.source,
                "enabled_by_default": event.enabled_by_default,
            },
        )


def downgrade() -> None:
    # Reversible only while nothing FKs into cultural_events (Phase 6 adds
    # posts.cultural_event_id). Deletes exactly the slugs this upgrade would
    # insert so alembic upgrade/downgrade round-trips cleanly in CI; once a
    # later phase references these rows, this step must become a raise.
    from app.features.cultural_events.catalog import build_events

    events = build_events()
    if not events:
        return
    conn = op.get_bind()
    for event in events:
        conn.execute(
            sa.text("DELETE FROM cultural_events WHERE slug = :slug"),
            {"slug": event.slug},
        )

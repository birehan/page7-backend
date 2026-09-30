"""Organization purge job — Phase 15 PDPL deletion.

Deliberately *not* a bare `DELETE FROM organizations` cascade:

- `invoices` are retained (Saudi tax / commercial-record retention).
- `audit_logs` and `ai_decisions` rows are retained as facts, but personal-data
  fields matching architecture/05 §10's redaction list are anonymized.
- The `organizations` row itself is kept as a tombstone so invoice FKs remain
  valid; `deleted_at` was set when the owner called DELETE /orgs/:orgId.
- Everything else tenant-scoped is hard-deleted.

See docs/phases/phase-15-production-hardening-and-launch.md §Database changes.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import text

from app.db.session import get_session_factory

log = structlog.get_logger(__name__)


async def handle_purge_organization(payload: dict[str, Any]) -> None:
    raw_id = payload.get("organization_id")
    if not raw_id:
        raise ValueError("maintenance.purge_organization requires organization_id")
    organization_id = uuid.UUID(str(raw_id))

    async with get_session_factory()() as session:
        # Anonymize retained audit / AI decision rows (do not delete).
        await session.execute(
            text(
                """
                UPDATE audit_logs
                SET actor_name = '[redacted]',
                    actor_ref = '[redacted]',
                    ip = NULL,
                    actor_user_id = NULL,
                    meta = '{}'::jsonb
                WHERE organization_id = :organization_id
                """
            ),
            {"organization_id": organization_id},
        )
        await session.execute(
            text(
                """
                UPDATE ai_decisions
                SET actor_user_id = NULL,
                    provider_response = NULL,
                    input_summary = '{}'::jsonb,
                    output = '{}'::jsonb
                WHERE organization_id = :organization_id
                """
            ),
            {"organization_id": organization_id},
        )
        # ai_feedback.user_id is NOT NULL and the row must be retained
        # (architecture/02 never-deleted list); leave rows in place — the
        # referenced user account is not org-scoped and is not purged here.

        # Operational tenant data — brands CASCADE to brand-scoped children.
        # Table names are a fixed allowlist — never interpolated from payload.
        purge_tables = (
            "brands",
            "notifications",
            "invitations",
            "memberships",
            "sessions",
            "oauth_states",
            "saved_replies",
            "organization_cultural_event_settings",
            "ai_usage_counters",
            "subscriptions",
            "org_settings",
            "upload_intents",
            "runs",
            "idempotency_keys",
        )
        for table in purge_tables:
            await session.execute(
                text(f"DELETE FROM {table} WHERE organization_id = :organization_id"),  # noqa: S608
                {"organization_id": organization_id},
            )

        # jobs.organization_id is nullable and has no FK — clear by hand.
        await session.execute(
            text("DELETE FROM jobs WHERE organization_id = :organization_id"),
            {"organization_id": organization_id},
        )

        await session.commit()
        log.info(
            "maintenance.purge_organization",
            organization_id=str(organization_id),
            retained=["invoices", "audit_logs", "ai_decisions", "organizations"],
        )

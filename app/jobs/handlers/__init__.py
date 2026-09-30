"""Job handler registry — maps type string to handler coroutine.

Each handler is `async def handle(payload: dict) -> None`.  It must be:
- Re-entrant (a job may be re-delivered on crash/fencing).
- Payload-agnostic (re-reads current domain state from the DB rather than
  trusting the enqueued values — architecture/02 §9).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.features.organizations.tasks import handle_purge_organization
from app.jobs.handlers.analytics import (
    handle_analytics_sync,
    handle_analytics_weekly_insights,
)
from app.jobs.handlers.billing import handle_billing_renew, handle_billing_usage_alerts
from app.jobs.handlers.brand_research import handle as handle_brand_research
from app.jobs.handlers.cleanup_upload_intents import handle as handle_cleanup_upload_intents
from app.jobs.handlers.cultural_calendar import handle_extend_catalog
from app.jobs.handlers.email_send import handle as handle_email_send
from app.jobs.handlers.inbox import (
    handle_inbox_classify_message,
    handle_inbox_poll,
    handle_inbox_send_reply,
    handle_inbox_sync,
    handle_reconcile_inbox_replies,
)
from app.jobs.handlers.maintenance import (
    handle_cleanup_sessions,
    handle_expire_idempotency_keys,
    handle_prune_jobs,
    handle_prune_rate_limits,
    handle_purge_run_events,
)
from app.jobs.handlers.plan_commit import handle as handle_plan_commit
from app.jobs.handlers.process_media import handle as handle_process_media
from app.jobs.handlers.publish_post import handle as handle_publish_post
from app.jobs.handlers.reconcile_publications import handle as handle_reconcile_publications
from app.jobs.handlers.social_accounts import (
    handle_process_webhook_event,
    handle_zernio_health_poll,
)
from app.jobs.handlers.sweep_publishing_invariants import (
    handle as handle_sweep_publishing_invariants,
)
from app.jobs.handlers.visuals import handle as handle_visuals_generate

Handler = Callable[[dict[str, Any]], Awaitable[None]]

REGISTRY: dict[str, Handler] = {
    "email.send": handle_email_send,
    "maintenance.cleanup_sessions": handle_cleanup_sessions,
    "maintenance.expire_idempotency_keys": handle_expire_idempotency_keys,
    "maintenance.prune_rate_limits": handle_prune_rate_limits,
    "maintenance.prune_jobs": handle_prune_jobs,
    "maintenance.purge_run_events": handle_purge_run_events,
    "maintenance.purge_organization": handle_purge_organization,
    "cultural_calendar.extend_catalog": handle_extend_catalog,
    "media.process": handle_process_media,
    "media.cleanup_upload_intents": handle_cleanup_upload_intents,
    "ai.plan_commit": handle_plan_commit,
    "ai.brand_research": handle_brand_research,
    "ai.visuals_generate": handle_visuals_generate,
    "publish_post": handle_publish_post,
    "reconcile_publications": handle_reconcile_publications,
    "sweep_publishing_invariants": handle_sweep_publishing_invariants,
    "zernio_health_poll": handle_zernio_health_poll,
    "process_webhook_event": handle_process_webhook_event,
    "analytics_sync": handle_analytics_sync,
    "analytics_weekly_insights": handle_analytics_weekly_insights,
    "inbox_sync": handle_inbox_sync,
    "inbox_poll": handle_inbox_poll,
    "inbox_classify_message": handle_inbox_classify_message,
    "inbox_send_reply": handle_inbox_send_reply,
    "reconcile_inbox_replies": handle_reconcile_inbox_replies,
    "billing.renew": handle_billing_renew,
    "billing.usage_alerts": handle_billing_usage_alerts,
}

__all__ = ["REGISTRY", "Handler"]

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.core.config import Environment, get_settings
from app.core.errors import install_exception_handlers
from app.core.logging import configure_logging
from app.core.request_context import RequestContextMiddleware
from app.core.security.csrf import CSRFMiddleware
from app.core.security.headers import SecurityHeadersMiddleware
from app.features.analytics.router import router as analytics_router
from app.features.audit.router import router as audit_router
from app.features.auth.router import router as auth_router
from app.features.auth.router import users_router
from app.features.billing.router import router as billing_router
from app.features.brand_research.router import router as brand_research_router
from app.features.brands.router import router as brands_router
from app.features.content_ai.router import router as content_ai_router
from app.features.cultural_events.router import router as cultural_events_router
from app.features.health.router import router as health_router
from app.features.inbox.router import router as inbox_router
from app.features.media.local_storage_router import router as local_storage_router
from app.features.media.router import router as media_router
from app.features.notifications.router import router as notifications_router
from app.features.organizations.router import router as organizations_router
from app.features.posts.router import router as posts_router
from app.features.publishing.router import brand_router as publishing_brand_router
from app.features.publishing.router import posts_actions_router as publishing_posts_router
from app.features.review_links.router import router as review_links_router
from app.features.review_links.router_public import router as review_links_public_router
from app.features.social_accounts.router import brand_router as channels_router
from app.features.social_accounts.router import callback_router as zernio_callback_router
from app.features.social_accounts.router import (
    capabilities_router as channels_capabilities_router,
)
from app.features.social_accounts.router import webhook_router as zernio_webhook_router
from app.features.strategy.router import router as strategy_router
from app.features.team.router import router as team_router
from app.features.visuals.router import router as visuals_router
from app.features.waitlist.router import router as waitlist_router
from app.infrastructure.telemetry.sentry import init_sentry
from app.infrastructure.telemetry.tracing import init_tracing


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(json_output=settings.app_env is not Environment.DEVELOPMENT)
    init_sentry(settings)

    app = FastAPI(title="pgblank.ai API", version="0.1.0")

    # Order matters: security headers and request-id wrap every response,
    # including error responses; CORS sits outermost so preflight requests never
    # reach application code.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    install_exception_handlers(app)
    init_tracing(app, settings)

    # Unversioned: infra-level load balancer / container health checks.
    app.include_router(health_router)
    # LocalStorage PUT/GET surface — only answers under development/testing.
    app.include_router(local_storage_router)

    # Empty in Phase 1 — every domain feature mounts here starting Phase 2.
    v1_router = APIRouter(prefix="/v1")
    v1_router.include_router(audit_router)
    v1_router.include_router(auth_router)
    v1_router.include_router(users_router)
    v1_router.include_router(organizations_router)
    v1_router.include_router(notifications_router)
    v1_router.include_router(team_router)
    v1_router.include_router(brands_router)
    v1_router.include_router(content_ai_router)
    v1_router.include_router(brand_research_router)
    v1_router.include_router(strategy_router)
    v1_router.include_router(cultural_events_router)
    v1_router.include_router(media_router)
    v1_router.include_router(posts_router)
    v1_router.include_router(publishing_posts_router)
    v1_router.include_router(publishing_brand_router)
    v1_router.include_router(review_links_router)
    v1_router.include_router(review_links_public_router)
    v1_router.include_router(channels_router)
    v1_router.include_router(channels_capabilities_router)
    v1_router.include_router(zernio_callback_router)
    v1_router.include_router(zernio_webhook_router)
    v1_router.include_router(visuals_router)
    v1_router.include_router(analytics_router)
    v1_router.include_router(inbox_router)
    v1_router.include_router(billing_router)
    v1_router.include_router(waitlist_router)
    app.include_router(v1_router)

    return app


app = create_app()

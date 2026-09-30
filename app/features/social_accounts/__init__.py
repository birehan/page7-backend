"""Social accounts and Zernio feature — Phase 9.

Eager exports are limited to models so integration layers (credential_pool)
can import ORM types without pulling router → service → integrations.social
into a circular import. Callers that need routers/service still go through
this barrel via ``__getattr__`` (architecture import-linter contracts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.features.social_accounts.models import SocialAccount, ZernioCredential

if TYPE_CHECKING:
    from app.features.social_accounts.router import (
        brand_router as brand_router,
    )
    from app.features.social_accounts.router import (
        callback_router as callback_router,
    )
    from app.features.social_accounts.router import (
        capabilities_router as capabilities_router,
    )
    from app.features.social_accounts.router import (
        router as router,
    )
    from app.features.social_accounts.router import (
        webhook_router as webhook_router,
    )
    from app.features.social_accounts.service import (
        capability_matrix as capability_matrix,
    )
    from app.features.social_accounts.service import (
        create_placeholders_for_brand as create_placeholders_for_brand,
    )
    from app.features.social_accounts.service import (
        get_account as get_account,
    )
    from app.features.social_accounts.service import (
        get_account_by_platform as get_account_by_platform,
    )
    from app.features.social_accounts.service import (
        get_account_by_zernio_id as get_account_by_zernio_id,
    )
    from app.features.social_accounts.service import (
        get_credential as get_credential,
    )
    from app.features.social_accounts.service import (
        soft_delete_for_brand as soft_delete_for_brand,
    )

__all__ = [
    "SocialAccount",
    "ZernioCredential",
    "brand_router",
    "callback_router",
    "capabilities_router",
    "capability_matrix",
    "create_placeholders_for_brand",
    "get_account",
    "get_account_by_platform",
    "get_account_by_zernio_id",
    "get_credential",
    "router",
    "soft_delete_for_brand",
    "webhook_router",
]


def __getattr__(name: str) -> Any:
    if name in {
        "brand_router",
        "callback_router",
        "capabilities_router",
        "router",
        "webhook_router",
    }:
        from app.features.social_accounts import router as _router

        return getattr(_router, name)
    if name in {
        "capability_matrix",
        "create_placeholders_for_brand",
        "get_account",
        "get_account_by_platform",
        "get_account_by_zernio_id",
        "get_credential",
        "soft_delete_for_brand",
    }:
        from app.features.social_accounts import service as _service

        return getattr(_service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

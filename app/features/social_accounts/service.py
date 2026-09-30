"""Social accounts / Zernio connect-flow orchestration (Phase 9)."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.time import utc_now
from app.features import auth as auth_feature
from app.features import notifications as notifications_feature
from app.features.social_accounts import repository
from app.features.social_accounts.exceptions import (
    CredentialPoolExhausted,
)
from app.features.social_accounts.models import SocialAccount, ZernioCredential, ZernioProfile
from app.features.social_accounts.schemas import (
    CapabilityMatrixOut,
    ChannelOAuthUrlOut,
    Platform,
    PlatformCapability,
    SocialAccountOut,
)
from app.integrations.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNotFoundError,
    ProviderPaymentRequiredError,
    ProviderUnavailableError,
)
from app.integrations.social import (
    AccountHealth,
    AccountInfo,
    _use_fake,
    credential_aliases,
    get_social_provider_for_credential,
    resolve_zernio_secret,
    select_for_connect,
    webhook_secret_ref_for_alias,
)
from app.jobs import queue as job_queue

log = structlog.get_logger(__name__)

PLATFORMS = repository.PLATFORMS
_OAUTH_STATE_TTL = timedelta(minutes=10)
_EXPIRING_DAYS = 7
_NOTIFY_THRESHOLDS = (7, 3, 1)
# FakeSocialProvider profile IDs look like prof_1_a1b2c3d4 — never valid on Zernio.
_FAKE_ZERNIO_PROFILE_ID = re.compile(r"^prof_\d+_[0-9a-f]{8}$")

_CAPABILITY_ROWS: tuple[tuple[Platform, bool, bool, str | None], ...] = (
    ("instagram", True, True, None),
    ("facebook", True, True, None),
    (
        "tiktok",
        True,
        False,
        "Gated until Phase 10 composer settings UI",
    ),
    (
        "snapchat",
        False,
        False,
        "Coming soon",
    ),
    (
        "whatsapp",
        False,
        False,
        "Not a Zernio publish target",
    ),
)


def capability_matrix() -> CapabilityMatrixOut:
    return CapabilityMatrixOut(
        platforms=[
            PlatformCapability(
                platform=platform,
                connectable=connectable,
                publishable=publishable,
                notes=notes,
            )
            for platform, connectable, publishable, notes in _CAPABILITY_ROWS
        ]
    )


async def get_account(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    account_id: uuid.UUID,
) -> SocialAccount | None:
    """Brand-scoped account lookup (publishing / jobs)."""
    return await repository.get_account(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        account_id=account_id,
    )


async def get_account_by_zernio_id(
    session: AsyncSession, *, zernio_account_id: str
) -> SocialAccount | None:
    """Resolve a social account by Zernio account id (webhook routing)."""
    return await repository.get_account_by_zernio_id(session, zernio_account_id=zernio_account_id)


async def get_account_by_platform(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    platform: str,
) -> SocialAccount | None:
    """Brand+platform account lookup (publishing claim path)."""
    return await repository.get_account_by_platform(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        platform=platform,
    )


async def get_credential(
    session: AsyncSession, *, credential_id: uuid.UUID
) -> ZernioCredential | None:
    """Credential lookup for publish claim-time enabled checks."""
    return await repository.get_credential(session, credential_id=credential_id)


def _platform_connectable(platform: str) -> bool:
    for name, connectable, _publishable, _notes in _CAPABILITY_ROWS:
        if name == platform:
            return connectable
    return False


def account_to_out(account: SocialAccount) -> SocialAccountOut:
    token_expires: str | None = None
    if account.token_expires_at is not None:
        token_expires = account.token_expires_at.isoformat()
    connected_at = account.connected_at.isoformat() if account.connected_at is not None else ""
    return SocialAccountOut(
        id=account.id,
        organization_id=account.organization_id,
        brand_id=account.brand_id,
        platform=account.platform,
        status=account.status,
        handle=account.handle or "",
        token_expires_at=token_expires,
        connected_at=connected_at,
    )


def hash_oauth_state(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# Allowlisted frontend return paths after the Zernio OAuth round-trip. Encoded as a
# one-char prefix on the opaque state token so we don't need a DB column — never
# accept an arbitrary client URL (open-redirect guard).
_OAUTH_RETURN_PATHS: dict[str, str] = {
    "o": "/onboarding",
    "c": "/settings/channels",
}
_DEFAULT_OAUTH_RETURN_PATH = "/settings/channels"


def mint_oauth_state(*, return_path: str = _DEFAULT_OAUTH_RETURN_PATH) -> str:
    code = next(
        (k for k, path in _OAUTH_RETURN_PATHS.items() if path == return_path),
        "c",
    )
    return f"{code}.{secrets.token_urlsafe(32)}"


def oauth_return_path_from_state(raw: str) -> str:
    code, sep, _rest = raw.partition(".")
    if not sep:
        return _DEFAULT_OAUTH_RETURN_PATH
    return _OAUTH_RETURN_PATHS.get(code, _DEFAULT_OAUTH_RETURN_PATH)


def normalize_oauth_return_path(return_path: str | None) -> str:
    if return_path is not None and return_path in _OAUTH_RETURN_PATHS.values():
        return return_path
    return _DEFAULT_OAUTH_RETURN_PATH


def build_oauth_provider_redirect_url(*, callback_url: str, state: str) -> str:
    """Embed our oauth ``state`` in the URL Zernio redirects back to.

    Zernio's connect callback appends ``connected`` / ``profileId`` / … to
    ``redirect_url`` but does **not** echo an opaque state of our choosing.
    Putting ``state`` on the redirect base is how it survives the round-trip
    (architecture/10 §3).
    """
    sep = "&" if "?" in callback_url else "?"
    return f"{callback_url}{sep}{urlencode({'state': state})}"


def build_callback_redirect(
    origin: str,
    *,
    return_path: str = _DEFAULT_OAUTH_RETURN_PATH,
    connected: str | None = None,
    error: str | None = None,
) -> str:
    """Frontend redirect target — origin always from config, path allowlisted."""
    path = normalize_oauth_return_path(return_path)
    base = origin.rstrip("/") + path
    params: dict[str, str] = {}
    if error:
        params["error"] = error
    elif connected:
        params["connected"] = connected
    if not params:
        return base
    return f"{base}?{urlencode(params)}"


def verify_webhook_signature(*, body: bytes, signature: str | None, secret: str) -> bool:
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def create_placeholders_for_brand(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
) -> list[SocialAccountOut]:
    rows = await repository.create_placeholders(
        session, organization_id=organization_id, brand_id=brand_id
    )
    return [account_to_out(r) for r in rows]


async def soft_delete_for_brand(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    settings: Settings,
) -> None:
    accounts = await repository.list_accounts_for_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    for account in accounts:
        if account.zernio_account_id and account.credential_id:
            cred = await repository.get_credential(session, credential_id=account.credential_id)
            if cred is not None and cred.status != "disabled":
                provider = get_social_provider_for_credential(
                    settings, alias=cred.alias, secret_ref=cred.secret_ref
                )
                try:
                    await provider.delete_account(account.zernio_account_id)
                except ProviderError:
                    log.info(
                        "social_accounts.delete_upstream_missing",
                        account_id=str(account.id),
                        zernio_account_id=account.zernio_account_id,
                    )
        await repository.mark_account_disconnected(session, account=account, clear_pins=True)


async def list_channels(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
) -> list[SocialAccountOut]:
    rows = await repository.list_accounts_for_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if len(rows) < len(PLATFORMS):
        try:
            rows = await repository.create_placeholders(
                session, organization_id=organization_id, brand_id=brand_id
            )
        except IntegrityError as exc:
            # Stale session brandId (deleted / wrong org) — never 500 the channels page.
            raise ApiError("NOT_FOUND", "Brand not found", status_code=404) from exc
    return [account_to_out(r) for r in rows]


async def _ensure_credentials(session: AsyncSession, *, settings: Settings) -> None:
    await repository.ensure_credentials_from_env(
        session,
        aliases=credential_aliases(settings),
        max_profiles=settings.social.max_profiles,
        max_accounts=settings.social.max_accounts_per_credential,
    )
    if _use_fake(settings):
        return
    # Live Zernio is the free-tier source of truth; local pins can orphan or lag.
    await _sync_capacity_from_zernio(session, settings=settings)


async def _sync_capacity_from_zernio(
    session: AsyncSession, *, settings: Settings
) -> None:
    """Cross-check each configured key's account count against live Zernio.

    Used when the local pool looks exhausted — Zernio is the source of truth for
    free-tier slots, and keys added via env (e.g. t4) must be counted too.
    """
    if _use_fake(settings):
        return
    aliases = credential_aliases(settings)
    for alias in aliases:
        credential = await repository.get_credential_by_alias(session, alias=alias)
        if credential is None or credential.status != "active":
            continue
        try:
            provider = get_social_provider_for_credential(
                settings, alias=credential.alias, secret_ref=credential.secret_ref
            )
            remote = await provider.list_accounts()
        except RuntimeError as exc:
            # Secret missing for this alias — skip; do not disable the row.
            log.warning(
                "zernio_capacity_sync_skipped",
                alias=alias,
                error=str(exc),
            )
            continue
        except ProviderAuthError:
            await repository.disable_credential(
                session, credential=credential, notes="auth", auth=True
            )
            log.error("zernio_credential_disabled", alias=alias, reason="auth_sync")
            continue
        except ProviderError as exc:
            log.warning(
                "zernio_capacity_sync_failed",
                alias=alias,
                error=type(exc).__name__,
            )
            continue
        await repository.set_connected_accounts(
            session, credential=credential, count=len(remote)
        )
        log.info(
            "zernio_capacity_synced",
            alias=alias,
            connected_accounts=len(remote),
            max_accounts=credential.max_accounts,
        )


async def _create_profile_on_credential(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    credential: ZernioCredential,
    settings: Settings,
) -> Any:
    """Create a Zernio profile under ``credential`` and persist the pin."""
    provider = get_social_provider_for_credential(
        settings, alias=credential.alias, secret_ref=credential.secret_ref
    )
    remote = await provider.create_profile(
        f"brand-{brand_id}",
        description=f"org={organization_id}",
    )
    return await repository.insert_zernio_profile(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        credential_id=credential.id,
        zernio_profile_id=remote.id,
    )


def _is_fake_shaped_profile_id(profile_id: str) -> bool:
    return _FAKE_ZERNIO_PROFILE_ID.fullmatch(profile_id) is not None


async def _retire_zernio_profile(
    session: AsyncSession, *, profile: ZernioProfile, reason: str
) -> None:
    profile.deleted_at = utc_now()
    profile.status = "deleted"
    await session.flush()
    log.info(
        "zernio_profile_retired",
        profile_id=str(profile.id),
        zernio_profile_id=profile.zernio_profile_id,
        reason=reason,
    )


async def _pick_spare_profile_for_brand(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    settings: Settings,
    exclude_ids: set[uuid.UUID] | None = None,
) -> Any | None:
    """Return an existing brand profile whose key still has account slots."""
    excluded = exclude_ids or set()
    live = not _use_fake(settings)
    profiles = await repository.list_live_zernio_profiles(
        session, organization_id=organization_id, brand_id=brand_id
    )
    for profile in profiles:
        if live and _is_fake_shaped_profile_id(profile.zernio_profile_id):
            await _retire_zernio_profile(session, profile=profile, reason="fake_shaped_under_live")
            continue
        if profile.credential_id in excluded:
            continue
        credential = await repository.get_credential(session, credential_id=profile.credential_id)
        if credential is None or credential.status != "active":
            continue
        if credential.connected_accounts < credential.max_accounts:
            return profile
    return None


async def _ensure_zernio_profile_for_connect(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    settings: Settings,
    exclude_ids: set[uuid.UUID] | None = None,
) -> Any:
    """Pick or create a brand profile on a key with spare Zernio account slots."""
    await _ensure_credentials(session, settings=settings)
    excluded: set[uuid.UUID] = set(exclude_ids or ())
    live_synced = False

    spare = await _pick_spare_profile_for_brand(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        settings=settings,
        exclude_ids=excluded,
    )
    if spare is not None:
        return spare

    # Retry create_profile across keys on capacity/auth failures.
    while True:
        try:
            credential = await select_for_connect(
                session,
                fill_to_tier=settings.social.fill_to_tier,
                exclude_ids=excluded,
            )
        except CredentialPoolExhausted as exc:
            if not live_synced:
                # Local counters / parked keys can lie — re-check every configured
                # alias against Zernio once before failing the connect.
                await _sync_capacity_from_zernio(session, settings=settings)
                live_synced = True
                continue
            raise ApiError(
                "CREDENTIAL_POOL_EXHAUSTED",
                "No Zernio credential has available account capacity — add another API key",
                status_code=503,
            ) from exc

        existing = await repository.get_live_zernio_profile_for_credential(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            credential_id=credential.id,
        )
        if existing is not None:
            if not _use_fake(settings) and _is_fake_shaped_profile_id(existing.zernio_profile_id):
                await _retire_zernio_profile(
                    session, profile=existing, reason="fake_shaped_under_live"
                )
            else:
                return existing

        try:
            return await _create_profile_on_credential(
                session,
                organization_id=organization_id,
                brand_id=brand_id,
                credential=credential,
                settings=settings,
            )
        except ProviderAuthError:
            await repository.disable_credential(
                session, credential=credential, notes="auth", auth=True
            )
            log.error("zernio_credential_disabled", alias=credential.alias, reason="auth")
            excluded.add(credential.id)
            continue
        except ProviderPaymentRequiredError as exc:
            if exc.is_capacity:
                await repository.mark_credential_at_capacity(session, credential=credential)
                log.info(
                    "zernio_credential_at_capacity",
                    alias=credential.alias,
                    reason=exc.reason,
                )
            else:
                await repository.disable_credential(
                    session,
                    credential=credential,
                    notes="payment_required",
                    payment=True,
                )
                log.error(
                    "zernio_credential_disabled",
                    alias=credential.alias,
                    reason="payment",
                )
            excluded.add(credential.id)
            continue


async def _ensure_zernio_profile(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    settings: Settings,
) -> Any:
    """Back-compat: ensure at least one live profile (prefers spare capacity)."""
    return await _ensure_zernio_profile_for_connect(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        settings=settings,
    )


async def request_oauth_url(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    platform: str,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    settings: Settings,
    return_path: str | None = None,
) -> ChannelOAuthUrlOut:
    if platform not in PLATFORMS:
        raise ApiError("VALIDATION", f"Unknown platform: {platform}", status_code=422)
    if not _platform_connectable(platform):
        raise ApiError(
            "VALIDATION",
            f"Platform {platform} is not connectable",
            status_code=422,
        )
    resolved_return_path = normalize_oauth_return_path(return_path)

    account = await repository.get_account_by_platform(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        platform=platform,
    )
    if account is None:
        await repository.create_placeholders(
            session, organization_id=organization_id, brand_id=brand_id
        )
        account = await repository.get_account_by_platform(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            platform=platform,
        )
    if account is None:
        raise ApiError("NOT_FOUND", "Channel not found", status_code=404)

    await _ensure_credentials(session, settings=settings)

    excluded: set[uuid.UUID] = set()

    while True:
        profile = await _ensure_zernio_profile_for_connect(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            settings=settings,
            exclude_ids=excluded,
        )
        credential = await repository.get_credential(session, credential_id=profile.credential_id)
        if credential is None or credential.status == "disabled":
            excluded.add(profile.credential_id)
            continue
        if credential.connected_accounts >= credential.max_accounts:
            excluded.add(credential.id)
            continue

        raw_state = mint_oauth_state(return_path=resolved_return_path)
        state_hash = hash_oauth_state(raw_state)
        await auth_feature.create_oauth_state(
            session,
            state_hash=state_hash,
            organization_id=organization_id,
            brand_id=brand_id,
            platform=platform,
            user_id=user_id,
            session_id=session_id,
            expires_at=utc_now() + _OAUTH_STATE_TTL,
        )

        provider = get_social_provider_for_credential(
            settings, alias=credential.alias, secret_ref=credential.secret_ref
        )
        try:
            connect = await provider.get_connect_url(
                platform,
                profile.zernio_profile_id,
                build_oauth_provider_redirect_url(
                    callback_url=settings.social.oauth_callback_url,
                    state=raw_state,
                ),
            )
        except ProviderPaymentRequiredError as exc:
            if exc.is_capacity:
                await repository.mark_credential_at_capacity(session, credential=credential)
                log.info(
                    "zernio_connect_at_capacity",
                    alias=credential.alias,
                    platform=platform,
                )
                excluded.add(credential.id)
                continue
            await repository.disable_credential(
                session,
                credential=credential,
                notes="payment_required",
                payment=True,
            )
            excluded.add(credential.id)
            continue
        except ProviderAuthError:
            await repository.disable_credential(
                session, credential=credential, notes="auth", auth=True
            )
            excluded.add(credential.id)
            log.error("zernio_credential_disabled", alias=credential.alias, reason="auth")
            continue
        except ProviderNotFoundError:
            await _retire_zernio_profile(
                session, profile=profile, reason="connect_profile_not_found"
            )
            excluded.add(profile.credential_id)
            continue

        account.zernio_profile_id = profile.id
        account.credential_id = profile.credential_id
        await session.flush()

        await repository.insert_connect_attempt(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            social_account_id=account.id,
            kind="connect",
            actor_user_id=user_id,
            oauth_state_hash=state_hash,
            zernio_profile_id=profile.id,
        )

        return ChannelOAuthUrlOut(authorize_url=connect.auth_url, state=raw_state)


@dataclass(frozen=True)
class RedirectResult:
    url: str


async def handle_oauth_callback(
    session: AsyncSession,
    *,
    state: str,
    session_id: uuid.UUID | None,
    connected: str | None,
    error: str | None,
    profile_id: str | None,
    account_id: str | None,
    username: str | None,
    settings: Settings,
) -> RedirectResult:
    origin = settings.social.callback_redirect_origin
    return_path = oauth_return_path_from_state(state)

    def redirect(*, connected: str | None = None, error: str | None = None) -> RedirectResult:
        return RedirectResult(
            url=build_callback_redirect(
                origin,
                return_path=return_path,
                connected=connected,
                error=error,
            )
        )

    if session_id is None:
        return redirect(error="unauthorized")

    info = await auth_feature.consume_oauth_state(
        session, state_hash=hash_oauth_state(state), session_id=session_id
    )
    if info is None:
        return redirect(error="invalid_state")

    if error:
        return redirect(error=error or "oauth_failed")

    if not connected and not account_id:
        return redirect(error="oauth_failed")

    account = await repository.get_account_by_platform(
        session,
        organization_id=info.organization_id,
        brand_id=info.brand_id,
        platform=info.platform,
    )
    if account is None:
        return redirect(error="not_found")

    profile = None
    if account.zernio_profile_id is not None:
        profile = await session.get(ZernioProfile, account.zernio_profile_id)
        if profile is not None and profile.deleted_at is not None:
            profile = None
    if profile is None and profile_id:
        # Match callback's Zernio profile id against our pins.
        for candidate in await repository.list_live_zernio_profiles(
            session,
            organization_id=info.organization_id,
            brand_id=info.brand_id,
        ):
            if candidate.zernio_profile_id == profile_id:
                profile = candidate
                break
    if profile is None:
        profile = await repository.get_live_zernio_profile(
            session,
            organization_id=info.organization_id,
            brand_id=info.brand_id,
            credential_id=account.credential_id,
        )
    if profile is None:
        return redirect(error="no_profile")

    credential = await repository.get_credential(session, credential_id=profile.credential_id)
    if credential is None:
        return redirect(error="no_credential")

    provider = get_social_provider_for_credential(
        settings, alias=credential.alias, secret_ref=credential.secret_ref
    )
    try:
        remote_accounts = await provider.list_accounts(
            profile_id=profile.zernio_profile_id, platform=info.platform
        )
    except ProviderError:
        remote_accounts = []

    match: AccountInfo | None = None
    if account_id:
        match = next((a for a in remote_accounts if a.id == account_id), None)
    if match is None and remote_accounts:
        match = remote_accounts[0]
    if match is not None:
        await repository.apply_connected_from_provider(
            session,
            account=account,
            zernio_account_id=match.id,
            handle=username or match.username,
            display_name=match.display_name,
            avatar_url=match.profile_picture,
            zernio_profile_id=profile.id,
            credential_id=profile.credential_id,
            connected_by=info.user_id,
        )
        from app.features import inbox as inbox_feature

        await inbox_feature.enqueue_inbox_sync(
            session,
            social_account_id=account.id,
            organization_id=account.organization_id,
        )

    platform_label = connected or info.platform
    return redirect(connected=platform_label)


async def sync_from_provider(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    account_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    kind: str,
    settings: Settings,
) -> SocialAccountOut:
    account = await repository.get_account(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        account_id=account_id,
    )
    if account is None:
        raise ApiError("NOT_FOUND", "Channel not found", status_code=404)

    if account.zernio_profile_id is not None and account.credential_id is not None:
        profile = await session.get(ZernioProfile, account.zernio_profile_id)
        if profile is None or profile.deleted_at is not None:
            profile = await repository.get_live_zernio_profile(
                session,
                organization_id=organization_id,
                brand_id=brand_id,
                credential_id=account.credential_id,
            )
    else:
        profile = await repository.get_live_zernio_profile(
            session, organization_id=organization_id, brand_id=brand_id
        )
    if profile is None:
        profile = await _ensure_zernio_profile(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            settings=settings,
        )

    credential = await repository.get_credential(session, credential_id=profile.credential_id)
    if credential is None or credential.status == "disabled":
        raise ApiError(
            "CREDENTIAL_POOL_EXHAUSTED",
            "Zernio credential unavailable",
            status_code=503,
        )

    attempt = await repository.insert_connect_attempt(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        social_account_id=account.id,
        kind=kind,
        actor_user_id=actor_user_id,
        zernio_profile_id=profile.id,
    )

    provider = get_social_provider_for_credential(
        settings, alias=credential.alias, secret_ref=credential.secret_ref
    )
    try:
        remote_accounts = await provider.list_accounts(
            profile_id=profile.zernio_profile_id, platform=account.platform
        )
    except ProviderPaymentRequiredError as exc:
        if exc.is_capacity:
            await repository.mark_credential_at_capacity(session, credential=credential)
            await repository.finish_connect_attempt(
                session,
                attempt=attempt,
                status="failed",
                error_code="CREDENTIAL_POOL_EXHAUSTED",
            )
            raise ApiError(
                "CREDENTIAL_POOL_EXHAUSTED",
                "Zernio key has no spare account capacity",
                status_code=503,
            ) from exc
        await repository.disable_credential(
            session, credential=credential, notes="payment_required", payment=True
        )
        await repository.finish_connect_attempt(
            session, attempt=attempt, status="failed", error_code="PROVIDER_PAYMENT_REQUIRED"
        )
        raise ApiError(
            "PROVIDER_PAYMENT_REQUIRED",
            "Zernio billing required",
            status_code=402,
        ) from exc
    except ProviderAuthError as exc:
        await repository.disable_credential(session, credential=credential, notes="auth", auth=True)
        await repository.finish_connect_attempt(
            session, attempt=attempt, status="failed", error_code="ACCOUNT_ISSUE"
        )
        raise ApiError(
            "ACCOUNT_ISSUE",
            "Zernio credential rejected",
            status_code=502,
        ) from exc

    match: AccountInfo | None = None
    if account.zernio_account_id:
        match = next((a for a in remote_accounts if a.id == account.zernio_account_id), None)
    if match is None and remote_accounts:
        match = remote_accounts[0]

    if match is None:
        await repository.finish_connect_attempt(
            session,
            attempt=attempt,
            status="failed",
            error_code="CHANNEL_NOT_CONNECTED",
            error_message="No connected account found at provider",
        )
        raise ApiError(
            "CHANNEL_NOT_CONNECTED",
            "No connected account found at provider",
            status_code=409,
        )

    await repository.apply_connected_from_provider(
        session,
        account=account,
        zernio_account_id=match.id,
        handle=match.username,
        display_name=match.display_name,
        avatar_url=match.profile_picture,
        zernio_profile_id=profile.id,
        credential_id=profile.credential_id,
        connected_by=actor_user_id,
    )
    await repository.finish_connect_attempt(session, attempt=attempt, status="succeeded")
    from app.features import inbox as inbox_feature

    await inbox_feature.enqueue_inbox_sync(
        session,
        social_account_id=account.id,
        organization_id=account.organization_id,
    )
    return account_to_out(account)


async def disconnect_channel(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    account_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    settings: Settings,
) -> SocialAccountOut:
    account = await repository.get_account(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        account_id=account_id,
    )
    if account is None:
        raise ApiError("NOT_FOUND", "Channel not found", status_code=404)

    attempt = await repository.insert_connect_attempt(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        social_account_id=account.id,
        kind="disconnect",
        actor_user_id=actor_user_id,
        zernio_profile_id=account.zernio_profile_id,
    )

    if account.zernio_account_id and account.credential_id:
        cred = await repository.get_credential(session, credential_id=account.credential_id)
        if cred is None:
            await repository.finish_connect_attempt(
                session,
                attempt=attempt,
                status="failed",
                error_code="ACCOUNT_ISSUE",
            )
            raise ApiError(
                "ACCOUNT_ISSUE",
                "Zernio credential missing for this channel",
                status_code=502,
            )
        # Still call DELETE when the credential is disabled — the API key may
        # work and we must free the upstream account (capacity + truth).
        provider = get_social_provider_for_credential(
            settings, alias=cred.alias, secret_ref=cred.secret_ref
        )
        try:
            await provider.delete_account(account.zernio_account_id)
        except ProviderError as exc:
            await repository.finish_connect_attempt(
                session,
                attempt=attempt,
                status="failed",
                error_code="PROVIDER_UNAVAILABLE",
                error_message=str(exc),
            )
            code = (
                "PROVIDER_UNAVAILABLE"
                if isinstance(exc, ProviderUnavailableError)
                else "ACCOUNT_ISSUE"
            )
            raise ApiError(
                code,
                "Could not disconnect the account from Zernio — try again",
                status_code=502,
            ) from exc

    await repository.mark_account_disconnected(session, account=account, clear_pins=False)
    await repository.finish_connect_attempt(session, attempt=attempt, status="succeeded")
    return account_to_out(account)


@dataclass
class HealthStatusChange:
    account: SocialAccount
    previous_status: str
    new_status: str
    notify_disconnected: bool
    expiring_days: int | None


def map_health_to_status(
    *,
    current_status: str,
    health: AccountHealth,
    now: datetime | None = None,
) -> str:
    """Map provider health onto social_accounts.status (architecture/10 §8)."""
    if current_status == "disconnected":
        return "disconnected"
    if health.needs_reconnect or not health.token_valid:
        return "expired"
    expires = _parse_token_expiry(health.token_expires_at)
    if expires is not None:
        instant = now or utc_now()
        if expires <= instant + timedelta(days=_EXPIRING_DAYS):
            return "expiring"
    return "connected"


def _parse_token_expiry(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        from datetime import UTC

        return parsed.replace(tzinfo=UTC)
    return parsed


def days_until_expiry(
    token_expires_at: datetime | None, *, now: datetime | None = None
) -> int | None:
    if token_expires_at is None:
        return None
    instant = now or utc_now()
    delta = token_expires_at - instant
    return max(0, delta.days)


async def apply_account_health(
    session: AsyncSession,
    *,
    account: SocialAccount,
    health: AccountHealth,
    user_initiated_disconnect: bool = False,
) -> HealthStatusChange | None:
    previous = account.status
    new_status = map_health_to_status(current_status=previous, health=health)
    expires = _parse_token_expiry(health.token_expires_at)
    account.token_expires_at = expires
    account.last_token_check_at = utc_now()
    if health.needs_reconnect or not health.token_valid:
        account.last_error_code = "ACCOUNT_TOKEN_EXPIRED"
        account.last_error_at = utc_now()

    if new_status == previous:
        await session.flush()
        return None

    account.status = new_status
    await session.flush()

    notify_disconnected = (
        not user_initiated_disconnect
        and previous in ("connected", "expiring")
        and new_status in ("expired", "disconnected")
    )
    days = days_until_expiry(expires)
    expiring_days: int | None = None
    if new_status == "expiring" and days is not None:
        for threshold in _NOTIFY_THRESHOLDS:
            if days <= threshold:
                expiring_days = threshold
                break

    return HealthStatusChange(
        account=account,
        previous_status=previous,
        new_status=new_status,
        notify_disconnected=notify_disconnected,
        expiring_days=expiring_days,
    )


async def maybe_notify_health(
    session: AsyncSession,
    *,
    change: HealthStatusChange,
) -> None:
    account = change.account
    href = f"/settings/channels?account={account.id}"
    if change.notify_disconnected:
        exists = await repository.notification_exists_for_threshold(
            session,
            organization_id=account.organization_id,
            brand_id=account.brand_id,
            account_id=account.id,
            notif_type="channel_disconnected",
        )
        if not exists:
            await notifications_feature.fan_out(
                session,
                organization_id=account.organization_id,
                brand_id=account.brand_id,
                type="channel_disconnected",
                message_key="channel_disconnected",
                params={
                    "accountId": str(account.id),
                    "platform": account.platform,
                    "handle": account.handle,
                },
                target_href=href,
                actor_user_id=None,
                actor_ref="system:zernio_health",
            )

    if change.expiring_days is not None and change.new_status == "expiring":
        exists = await repository.notification_exists_for_threshold(
            session,
            organization_id=account.organization_id,
            brand_id=account.brand_id,
            account_id=account.id,
            notif_type="channel_expiring",
            days=change.expiring_days,
        )
        if not exists:
            await notifications_feature.fan_out(
                session,
                organization_id=account.organization_id,
                brand_id=account.brand_id,
                type="channel_expiring",
                message_key="channel_expiring",
                params={
                    "accountId": str(account.id),
                    "platform": account.platform,
                    "handle": account.handle,
                    "days": change.expiring_days,
                },
                target_href=href,
                actor_user_id=None,
                actor_ref="system:zernio_health",
            )


async def ingest_webhook(
    session: AsyncSession,
    *,
    alias: str,
    body: bytes,
    signature: str | None,
    external_event_id_header: str | None,
    settings: Settings,
) -> dict[str, bool]:
    aliases = set(settings.social.credential_aliases)
    raw_env = __import__("os").environ.get("ZERNIO_CREDENTIAL_ALIASES")
    if raw_env and raw_env.strip():
        aliases = {part.strip() for part in raw_env.split(",") if part.strip()}
    if alias not in aliases:
        # Also accept aliases present in DB (seeded credentials).
        cred = await repository.get_credential_by_alias(session, alias=alias)
        if cred is None:
            raise ApiError("NOT_FOUND", "Unknown webhook alias", status_code=404)

    if len(body) > 1_000_000:
        raise ApiError("PAYLOAD_TOO_LARGE", "Webhook body too large", status_code=413)

    secret_ref = webhook_secret_ref_for_alias(alias)
    try:
        secret = resolve_zernio_secret(secret_ref)
    except RuntimeError as exc:
        raise ApiError(
            "PROVIDER_UNAVAILABLE",
            "Webhook secret not configured",
            status_code=503,
        ) from exc

    if not verify_webhook_signature(body=body, signature=signature, secret=secret):
        log.warning("webhook_signature_invalid", alias=alias)
        raise ApiError("UNAUTHORIZED", "Invalid webhook signature", status_code=401)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ApiError("VALIDATION", "Invalid JSON body", status_code=422) from exc
    if not isinstance(payload, dict):
        raise ApiError("VALIDATION", "Webhook payload must be an object", status_code=422)

    external_id = (
        str(payload.get("id") or "")
        or (external_event_id_header or "")
        or hashlib.sha256(body).hexdigest()
    )
    event_type = str(payload.get("type") or payload.get("event") or "unknown")

    event_id = await repository.insert_webhook_event(
        session,
        provider="zernio",
        external_event_id=external_id,
        event_type=event_type,
        payload=payload,
        signature_valid=True,
    )
    if event_id is not None:
        await job_queue.enqueue(
            session,
            queue="webhooks",
            type="process_webhook_event",
            payload={"webhook_event_id": event_id, "alias": alias},
            unique_key=f"webhook:{event_id}",
        )
    return {"ok": True}

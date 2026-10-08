from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Environment, Settings, get_settings
from app.core.errors import ApiError
from app.core.request_context import AuthenticatedUser, require_session
from app.core.security.client_ip import client_ip
from app.core.time import utc_now
from app.db.session import get_db_session
from app.features import team
from app.features.auth import service
from app.features.auth.models import User
from app.features.auth.schemas import (
    AcceptInviteBody,
    AuthenticatedLoginOut,
    AuthProvidersOut,
    EmailVerificationRequiredOut,
    ForgotPasswordBody,
    GoogleStartOut,
    InvitePreviewOut,
    LoginBody,
    LoginResponseOut,
    MfaEnrollResponseOut,
    MfaRequiredLoginOut,
    MfaVerifyBody,
    ResendVerificationBody,
    ResetPasswordBody,
    SessionPayloadOut,
    SessionUserOut,
    SignupBody,
    SignupResponseOut,
    UpdateUserBody,
    UserOut,
    VerifyEmailBody,
)
from app.features.auth.service import (
    AuthResult,
    EmailVerificationRequiredResult,
    MfaChallengeResult,
)

router = APIRouter(tags=["auth"])
users_router = APIRouter(prefix="/users", tags=["users"])


def _client_ip(request: Request) -> str | None:
    return client_ip(request)


def _to_session_payload(result: AuthResult) -> SessionPayloadOut:
    org = result.organization
    return SessionPayloadOut(
        user=SessionUserOut(
            id=result.user.id,
            name=result.user.name,
            name_ar=result.user.name_ar,
            email=result.user.email,
            role=result.role,
            avatar_url=result.user.avatar_url,
        ),
        organization_id=org.id,
        organization_name=org.name,
        organization_industry=org.industry,
        organization_city=org.city,
        organization_vat_number=org.vat_number or "",
        expires_at=int(result.session.expires_at.timestamp() * 1000),
        mfa_enabled=result.user.mfa_enabled,
    )


def _set_session_cookie(response: Response, settings: Settings, result: AuthResult) -> None:
    max_age = int((result.session.expires_at - utc_now()).total_seconds())
    secure = settings.app_env is not Environment.DEVELOPMENT
    samesite = settings.auth.cookie_samesite
    if samesite == "none" and not secure:
        raise RuntimeError("AUTH__COOKIE_SAMESITE=none requires Secure cookies")
    response.set_cookie(
        key=settings.auth.session_cookie_name,
        value=result.raw_token,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=secure,
        samesite=samesite,
    )


def _frontend_path(
    settings: Settings, locale: str, path: str, query: dict[str, str] | None = None
) -> str:
    base = settings.auth.frontend_url.rstrip("/")
    loc = locale if locale in ("ar", "en") else "en"
    url = f"{base}/{loc}{path}"
    if query:
        return f"{url}?{urlencode(query)}"
    return url


@router.get("/auth/me", response_model=SessionPayloadOut, response_model_exclude_none=True)
async def get_me(
    user: Annotated[AuthenticatedUser, Depends(require_session)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> SessionPayloadOut:
    me = await service.get_me(db, user_id=user.user_id)
    org = await service.get_session_organization(db, organization_id=user.organization_id)
    membership = await team.get_membership(
        db, organization_id=user.organization_id, user_id=user.user_id
    )
    role = membership.role if membership else "viewer"
    return SessionPayloadOut(
        user=SessionUserOut(
            id=me.id,
            name=me.name,
            name_ar=me.name_ar,
            email=me.email,
            role=role,
            avatar_url=me.avatar_url,
        ),
        organization_id=org.id,
        organization_name=org.name,
        organization_industry=org.industry,
        organization_city=org.city,
        organization_vat_number=org.vat_number or "",
        expires_at=int(user.expires_at.timestamp() * 1000),
        mfa_enabled=me.mfa_enabled,
    )


@router.get("/auth/providers", response_model=AuthProvidersOut)
async def auth_providers(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthProvidersOut:
    return AuthProvidersOut(google=service.google_providers_enabled(settings=settings))


@router.get("/auth/google/start", response_model=GoogleStartOut)
async def google_start(
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    remember_me: Annotated[bool, Query(alias="rememberMe")] = False,
    locale: Annotated[str, Query()] = "en",
) -> GoogleStartOut:
    result = await service.start_google_oauth(
        db, settings=settings, remember_me=remember_me, locale=locale
    )
    return GoogleStartOut(authorize_url=result.authorize_url)


@router.get("/auth/google/callback")
async def google_callback(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    if error or not code or not state:
        return RedirectResponse(
            url=_frontend_path(settings, "en", "/login", {"error": "google"}),
            status_code=status.HTTP_302_FOUND,
        )
    try:
        outcome = await service.finish_google_oauth(
            db,
            settings=settings,
            code=code,
            state=state,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except ApiError:
        return RedirectResponse(
            url=_frontend_path(settings, "en", "/login", {"error": "google"}),
            status_code=status.HTTP_302_FOUND,
        )

    if outcome.mfa_challenge is not None:
        return RedirectResponse(
            url=_frontend_path(
                settings,
                outcome.locale,
                "/login",
                {"mfaChallenge": outcome.mfa_challenge.raw_challenge_token},
            ),
            status_code=status.HTTP_302_FOUND,
        )

    assert outcome.auth_result is not None
    redirect = RedirectResponse(
        url=_frontend_path(
            settings,
            outcome.locale,
            "/onboarding" if outcome.is_new_user else "/dashboard",
        ),
        status_code=status.HTTP_302_FOUND,
    )
    _set_session_cookie(redirect, settings, outcome.auth_result)
    return redirect


@router.post("/auth/login", response_model=LoginResponseOut, response_model_exclude_none=True)
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> LoginResponseOut:
    result = await service.login(
        db,
        email=body.email,
        password=body.password,
        remember_me=body.remember_me,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if isinstance(result, MfaChallengeResult):
        return MfaRequiredLoginOut(challenge_token=result.raw_challenge_token)
    if isinstance(result, EmailVerificationRequiredResult):
        return EmailVerificationRequiredOut(
            challenge_token=result.raw_challenge_token,
            email=result.masked_email,
        )
    _set_session_cookie(response, settings, result)
    return AuthenticatedLoginOut(session=_to_session_payload(result))


@router.post("/auth/signup", response_model=SignupResponseOut, response_model_exclude_none=True)
async def signup(
    body: SignupBody,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> SignupResponseOut:
    result = await service.signup(
        db,
        name=body.name,
        email=body.email,
        password=body.password,
        organization_name=body.organization_name,
        locale=body.locale,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return EmailVerificationRequiredOut(
        challenge_token=result.raw_challenge_token,
        email=result.masked_email,
    )


@router.post(
    "/auth/verify-email",
    response_model=SessionPayloadOut,
    response_model_exclude_none=True,
)
async def verify_email(
    body: VerifyEmailBody,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> SessionPayloadOut:
    result = await service.verify_email(
        db,
        challenge_token=body.challenge_token,
        code=body.code,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookie(response, settings, result)
    return _to_session_payload(result)


@router.post(
    "/auth/resend-verification",
    response_model=EmailVerificationRequiredOut,
    response_model_exclude_none=True,
)
async def resend_verification(
    body: ResendVerificationBody,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> EmailVerificationRequiredOut:
    result = await service.resend_email_verification(
        db,
        challenge_token=body.challenge_token,
        ip=_client_ip(request),
    )
    return EmailVerificationRequiredOut(
        challenge_token=result.raw_challenge_token,
        email=result.masked_email,
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    user: Annotated[AuthenticatedUser, Depends(require_session)],
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    await service.logout(db, session_id=user.session_id)
    response.delete_cookie(key=settings.auth.session_cookie_name, path="/")


@router.post("/auth/forgot", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(
    body: ForgotPasswordBody,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    await service.forgot_password(db, email=body.email, ip=_client_ip(request))


@router.post("/auth/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    body: ResetPasswordBody, db: Annotated[AsyncSession, Depends(get_db_session)]
) -> None:
    await service.reset_password(db, token=body.token, new_password=body.password)


@router.get("/auth/invite/{token}", response_model=InvitePreviewOut)
async def get_invite_preview(
    token: str, db: Annotated[AsyncSession, Depends(get_db_session)]
) -> InvitePreviewOut:
    invitation = await service.get_invite_preview(db, token=token)
    org, inviter_name = await service.get_invite_preview_context(db, invitation=invitation)
    return InvitePreviewOut(
        organization_name=org.name,
        email=invitation.email,
        role=invitation.role,
        inviter_name=inviter_name,
        expires_at=invitation.expires_at.isoformat(),
    )


@router.post(
    "/auth/invite/{token}/accept",
    response_model=SessionPayloadOut,
    response_model_exclude_none=True,
)
async def accept_invite(
    token: str,
    body: AcceptInviteBody,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> SessionPayloadOut:
    result = await service.accept_invite(
        db,
        token=token,
        name=body.name,
        password=body.password,
        locale=body.locale,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookie(response, settings, result)
    return _to_session_payload(result)


@router.post("/auth/mfa/enroll", response_model=MfaEnrollResponseOut)
async def mfa_enroll(
    user: Annotated[AuthenticatedUser, Depends(require_session)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> MfaEnrollResponseOut:
    me = await service.get_me(db, user_id=user.user_id)
    result = await service.mfa_enroll(db, user=me)
    return MfaEnrollResponseOut(
        secret=result.secret,
        otpauth_url=result.otpauth_url,
        recovery_codes=result.recovery_codes,
    )


@router.post("/auth/mfa/verify", response_model=SessionPayloadOut, response_model_exclude_none=True)
async def mfa_verify(
    body: MfaVerifyBody,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> SessionPayloadOut:
    if body.challenge_token is not None:
        result = await service.mfa_verify_challenge(
            db,
            challenge_token=body.challenge_token,
            code=body.code,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        _set_session_cookie(response, settings, result)
        return _to_session_payload(result)

    user = await require_session(request, db, settings)
    me = await service.get_me(db, user_id=user.user_id)
    await service.mfa_verify_enrollment(db, user=me, code=body.code)
    org = await service.get_session_organization(db, organization_id=user.organization_id)
    membership = await team.get_membership(
        db, organization_id=user.organization_id, user_id=user.user_id
    )
    role = membership.role if membership else "viewer"
    return SessionPayloadOut(
        user=SessionUserOut(
            id=me.id,
            name=me.name,
            name_ar=me.name_ar,
            email=me.email,
            role=role,
            avatar_url=me.avatar_url,
        ),
        organization_id=org.id,
        organization_name=org.name,
        organization_industry=org.industry,
        organization_city=org.city,
        organization_vat_number=org.vat_number or "",
        expires_at=int(user.expires_at.timestamp() * 1000),
        mfa_enabled=me.mfa_enabled,
    )


# --- users/me ------------------------------------------------------------


def _to_user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        name=user.name,
        name_ar=user.name_ar,
        email=user.email,
        avatar_url=user.avatar_url,
    )


@users_router.get("/me", response_model=UserOut, response_model_exclude_none=True)
async def get_my_profile(
    user: Annotated[AuthenticatedUser, Depends(require_session)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> UserOut:
    me = await service.get_me(db, user_id=user.user_id)
    return _to_user_out(me)


@users_router.patch("/me", response_model=UserOut, response_model_exclude_none=True)
async def update_my_profile(
    body: UpdateUserBody,
    user: Annotated[AuthenticatedUser, Depends(require_session)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> UserOut:
    me = await service.update_me(
        db,
        user_id=user.user_id,
        name=body.name,
        name_ar=body.name_ar,
        avatar_url=body.avatar_url,
    )
    return _to_user_out(me)

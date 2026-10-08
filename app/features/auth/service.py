from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pyotp
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.security import crypto
from app.core.time import utc_now
from app.features import audit
from app.features.auth import repository
from app.features.auth.models import Invitation, MfaCredential, User
from app.features.auth.models import Session as AuthSession
from app.infrastructure.ratelimit.limiter import RateLimiter
from app.jobs import queue

if TYPE_CHECKING:
    # Type-only: the import-linter feature-boundary contract forbids
    # `app.features.organizations.models` outside `organizations` itself.
    # Every runtime value of this type comes back from a barrel call
    # (`organizations.get_organization`, `.create_organization_for_signup`)
    # — this import never executes, it only names the type those calls
    # return, for the dataclass fields and signatures below.
    from app.core.config import Settings
    from app.features.organizations.models import Organization

# Fixed lifetimes, set once at login: a session never extends itself while in use, and the
# cookie's max-age is fixed to match. A user is signed out when this elapses, active or not.
_SESSION_TTL_DEFAULT = timedelta(hours=12)
_SESSION_TTL_REMEMBER_ME = timedelta(days=30)
_RESET_TOKEN_TTL = timedelta(hours=1)
# Abuse limits, each counted per fixed window. Password-reset requests are counted whether or
# not the account exists, so hitting a limit never reveals who is registered.
_FORGOT_EMAIL_LIMIT = (3, timedelta(hours=1))
_FORGOT_IP_LIMIT = (10, timedelta(hours=1))
_SIGNUP_IP_LIMIT = (10, timedelta(hours=1))
_INVITE_TTL = timedelta(days=7)
_MFA_CHALLENGE_TTL = timedelta(minutes=5)
_EMAIL_VERIFY_TTL = timedelta(minutes=10)
_EMAIL_VERIFY_MAX_ATTEMPTS = 5
_EMAIL_VERIFY_RESEND_COOLDOWN = timedelta(seconds=60)
_EMAIL_VERIFY_SEND_WINDOW = timedelta(minutes=10)
_EMAIL_VERIFY_MAX_SENDS_PER_EMAIL = 3
_EMAIL_VERIFY_MAX_SENDS_PER_IP = 10
_AUTH_OAUTH_STATE_TTL = timedelta(minutes=10)
_LOGIN_LOCKOUT_THRESHOLD = 10
_LOGIN_LOCKOUT_WINDOW = timedelta(minutes=15)
_MFA_VERIFY_LOCKOUT_THRESHOLD = 5
_MFA_VERIFY_LOCKOUT_WINDOW = timedelta(minutes=5)
_RECOVERY_CODE_COUNT = 10
# Calibrated to roughly what a real Resend call costs — architecture/05 §1's
# enumeration-safety rule requires the known- and unknown-email branches of
# `/auth/forgot` to take comparable time; without this, skipping the email
# send on a miss would itself be a timing side-channel. Approximate by
# nature — narrower than a literal claim of indistinguishability under
# adversarial timing analysis, wider than doing nothing.
_FORGOT_PASSWORD_MISS_DELAY_SECONDS = 0.2


@dataclass
class AuthResult:
    user: User
    organization: Organization
    session: AuthSession
    raw_token: str
    role: str


@dataclass
class MfaChallengeResult:
    raw_challenge_token: str


@dataclass
class EmailVerificationRequiredResult:
    raw_challenge_token: str
    masked_email: str


@dataclass
class MfaEnrollResult:
    secret: str
    otpauth_url: str
    recovery_codes: list[str]


@dataclass
class GoogleStartResult:
    authorize_url: str


@dataclass
class GoogleFinishResult:
    """Outcome of finishing Google OAuth — session, MFA challenge, or redirect metadata."""

    locale: str
    is_new_user: bool
    auth_result: AuthResult | None = None
    mfa_challenge: MfaChallengeResult | None = None


def _new_token() -> str:
    """32 random bytes, base64url-encoded — architecture/02's PK-strategy
    rule for every token in this system (session, reset, invite, challenge).
    """
    return secrets.token_urlsafe(32)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_otp_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_otp_code(*, salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not local or not domain:
        return "***"
    visible = local[:1]
    return f"{visible}***@{domain}"


async def _enforce_rate_limit(bucket: str, limit: tuple[int, timedelta], message: str) -> None:
    """Count one hit against `bucket`; raise 429 once it is over the limit for the window."""
    threshold, window = limit
    count = await RateLimiter().increment(bucket=bucket, now=utc_now(), window=window)
    if count > threshold:
        raise ApiError("RATE_LIMIT", message, status_code=429)


async def _create_session_for(
    session: AsyncSession,
    *,
    user: User,
    organization: Organization,
    role: str,
    remember_me: bool,
    ip: str | None,
    user_agent: str | None,
) -> AuthResult:
    raw_token = _new_token()
    ttl = _SESSION_TTL_REMEMBER_ME if remember_me else _SESSION_TTL_DEFAULT
    row = await repository.create_session(
        session,
        user_id=user.id,
        organization_id=organization.id,
        token_hash=_hash_token(raw_token),
        expires_at=utc_now() + ttl,
        remember_me=remember_me,
        ip=ip,
        user_agent=user_agent,
    )
    return AuthResult(
        user=user, organization=organization, session=row, raw_token=raw_token, role=role
    )


# --- signup / login / logout --------------------------------------------


async def _enforce_email_verify_send_limits(
    *, email: str, ip: str | None
) -> None:
    limiter = RateLimiter()
    now = utc_now()
    email_bucket = f"email_verify:send:email:{hashlib.sha256(email.lower().encode()).hexdigest()}"
    email_count = await limiter.increment(
        bucket=email_bucket, now=now, window=_EMAIL_VERIFY_SEND_WINDOW
    )
    if email_count > _EMAIL_VERIFY_MAX_SENDS_PER_EMAIL:
        raise ApiError("RATE_LIMIT", "Too many verification emails", status_code=429)
    if ip:
        ip_count = await limiter.increment(
            bucket=f"email_verify:send:ip:{ip}", now=now, window=_EMAIL_VERIFY_SEND_WINDOW
        )
        if ip_count > _EMAIL_VERIFY_MAX_SENDS_PER_IP:
            raise ApiError("RATE_LIMIT", "Too many verification emails", status_code=429)


async def _issue_email_verification_challenge(
    session: AsyncSession,
    *,
    user: User,
    locale: str,
    ip: str | None,
) -> EmailVerificationRequiredResult:
    await _enforce_email_verify_send_limits(email=user.email, ip=ip)
    await repository.consume_active_email_verification_challenges(session, user_id=user.id)

    raw_token = _new_token()
    code = _new_otp_code()
    salt = secrets.token_hex(16)
    challenge = await repository.create_email_verification_challenge(
        session,
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        code_salt=salt,
        code_hash=_hash_otp_code(salt=salt, code=code),
        expires_at=utc_now() + _EMAIL_VERIFY_TTL,
        locale=locale if locale in ("ar", "en") else "en",
        ip=ip,
    )
    await queue.enqueue(
        session,
        queue="sync",
        type="email.send",
        payload={
            "kind": "verify_email",
            "challenge_id": str(challenge.id),
            "code": code,
            "locale": challenge.locale,
        },
        unique_key=f"verify_email:{challenge.id}",
    )
    return EmailVerificationRequiredResult(
        raw_challenge_token=raw_token,
        masked_email=_mask_email(user.email),
    )


async def signup(
    session: AsyncSession,
    *,
    name: str,
    email: str,
    password: str,
    organization_name: str,
    locale: str = "en",
    ip: str | None = None,
    user_agent: str | None = None,
) -> EmailVerificationRequiredResult:
    # Local import: features.team.create_membership, to avoid the
    # features.auth <-> features.team module-level circular import —
    # features/team/router.py makes the (safe) other-direction top-level
    # import of features.auth for its invite-touching endpoints.
    from app.features import organizations, team

    del user_agent  # reserved for future audit; signup issues no session yet

    if ip:
        await _enforce_rate_limit(f"signup:ip:{ip}", _SIGNUP_IP_LIMIT, "Too many sign-up attempts")

    existing = await repository.get_user_by_email(session, email)
    if existing is not None:
        if existing.email_verified_at is not None:
            raise ApiError("EMAIL_IN_USE", "Email is already in use", status_code=409)
        if existing.password_hash is None or not await crypto.verify_password_async(
            existing.password_hash, password
        ):
            raise ApiError("EMAIL_IN_USE", "Email is already in use", status_code=409)
        return await _issue_email_verification_challenge(
            session, user=existing, locale=locale, ip=ip
        )

    user = await repository.create_user(
        session,
        email=email,
        name=name,
        password_hash=await crypto.hash_password_async(password),
        locale=locale if locale in ("ar", "en") else "en",
    )
    org = await organizations.create_organization_for_signup(
        session, name=organization_name, created_by_user_id=user.id
    )
    await team.create_membership(session, organization_id=org.id, user_id=user.id, role="owner")
    await audit.record(
        session,
        organization_id=org.id,
        actor_kind="user",
        actor_ref=str(user.id),
        actor_name=user.name,
        action="org.created",
        target_type="organization",
        target_id=org.id,
    )
    return await _issue_email_verification_challenge(
        session, user=user, locale=locale, ip=ip
    )


async def verify_email(
    session: AsyncSession,
    *,
    challenge_token: str,
    code: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuthResult:
    from app.features import organizations, team

    challenge = await repository.get_email_verification_challenge_by_token_hash(
        session, _hash_token(challenge_token)
    )
    if (
        challenge is None
        or challenge.consumed_at is not None
        or challenge.expires_at < utc_now()
    ):
        raise ApiError("INVALID_TOKEN", "Verification code is invalid or expired", status_code=400)

    if challenge.attempt_count >= _EMAIL_VERIFY_MAX_ATTEMPTS:
        raise ApiError(
            "CODE_EXHAUSTED",
            "Too many incorrect codes. Request a new one.",
            status_code=400,
        )

    expected = _hash_otp_code(salt=challenge.code_salt, code=code)
    if not secrets.compare_digest(expected, challenge.code_hash):
        # Own transaction: ApiError rolls back the request session, so the
        # attempt counter must commit independently (same pattern as RateLimiter).
        count = await repository.record_email_verification_failure(
            challenge.id, max_attempts=_EMAIL_VERIFY_MAX_ATTEMPTS
        )
        if count >= _EMAIL_VERIFY_MAX_ATTEMPTS:
            raise ApiError(
                "CODE_EXHAUSTED",
                "Too many incorrect codes. Request a new one.",
                status_code=400,
            )
        raise ApiError("INVALID_CODE", "That code isn't right. Try again.", status_code=400)

    user = await repository.get_user_by_id(session, challenge.user_id)
    if user is None:
        raise ApiError("INVALID_TOKEN", "Verification code is invalid or expired", status_code=400)

    user.email_verified_at = utc_now()
    user.last_login_at = utc_now()
    await session.flush()
    await repository.mark_email_verification_challenge_consumed(session, challenge)
    await repository.consume_active_email_verification_challenges(session, user_id=user.id)

    membership = await team.get_default_membership(session, user.id)
    if membership is None:
        raise RuntimeError(f"user {user.id} has no organization")
    org = await organizations.get_organization(session, membership.organization_id)
    return await _create_session_for(
        session,
        user=user,
        organization=org,
        role=membership.role,
        remember_me=False,
        ip=ip,
        user_agent=user_agent,
    )


async def resend_email_verification(
    session: AsyncSession,
    *,
    challenge_token: str,
    ip: str | None = None,
) -> EmailVerificationRequiredResult:
    challenge = await repository.get_email_verification_challenge_by_token_hash(
        session, _hash_token(challenge_token)
    )
    if challenge is None or challenge.consumed_at is not None:
        raise ApiError("INVALID_TOKEN", "Verification challenge is invalid", status_code=400)

    if utc_now() - challenge.created_at < _EMAIL_VERIFY_RESEND_COOLDOWN:
        raise ApiError(
            "RESEND_TOO_SOON",
            "Wait a moment before requesting another code.",
            status_code=429,
        )

    user = await repository.get_user_by_id(session, challenge.user_id)
    if user is None:
        raise ApiError("INVALID_TOKEN", "Verification challenge is invalid", status_code=400)
    if user.email_verified_at is not None:
        raise ApiError("ALREADY_VERIFIED", "Email is already verified", status_code=400)

    return await _issue_email_verification_challenge(
        session, user=user, locale=challenge.locale, ip=ip
    )


async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    remember_me: bool,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuthResult | MfaChallengeResult | EmailVerificationRequiredResult:
    from app.features import organizations, team

    limiter = RateLimiter()
    now = utc_now()
    email_bucket = f"login:email:{hashlib.sha256(email.lower().encode()).hexdigest()}"
    email_count = await limiter.increment(
        bucket=email_bucket, now=now, window=_LOGIN_LOCKOUT_WINDOW
    )
    ip_count = 0
    if ip:
        ip_count = await limiter.increment(
            bucket=f"login:ip:{ip}", now=now, window=_LOGIN_LOCKOUT_WINDOW
        )
    if email_count > _LOGIN_LOCKOUT_THRESHOLD or ip_count > _LOGIN_LOCKOUT_THRESHOLD:
        raise ApiError("RATE_LIMIT", "Too many login attempts", status_code=429)

    user = await repository.get_user_by_email(session, email)
    if user is None or user.password_hash is None:
        # architecture/05 §1: the hasher still runs on a lookup miss, so
        # "wrong password" and "no such user" cost the same Argon2 verify —
        # never short-circuit straight to the error.
        await crypto.verify_password_async(crypto.DUMMY_PASSWORD_HASH, password)
        raise ApiError("INVALID_CREDENTIALS", "Invalid email or password", status_code=401)
    if not await crypto.verify_password_async(user.password_hash, password):
        raise ApiError("INVALID_CREDENTIALS", "Invalid email or password", status_code=401)

    if user.email_verified_at is None:
        return await _issue_email_verification_challenge(
            session, user=user, locale=user.locale or "en", ip=ip
        )

    user.last_login_at = utc_now()
    await session.flush()

    if user.mfa_enabled:
        raw_token = _new_token()
        await repository.create_mfa_challenge(
            session,
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=utc_now() + _MFA_CHALLENGE_TTL,
            remember_me=remember_me,
            ip=ip,
        )
        return MfaChallengeResult(raw_challenge_token=raw_token)

    membership = await team.get_default_membership(session, user.id)
    if membership is None:
        # Every user has at least one membership by construction (signup and
        # accept-invite both create one in the same transaction as the user).
        raise RuntimeError(f"user {user.id} has no organization")
    org = await organizations.get_organization(session, membership.organization_id)
    return await _create_session_for(
        session,
        user=user,
        organization=org,
        role=membership.role,
        remember_me=remember_me,
        ip=ip,
        user_agent=user_agent,
    )


async def logout(session: AsyncSession, *, session_id: uuid.UUID) -> None:
    from sqlalchemy import select

    row = (
        await session.execute(select(AuthSession).where(AuthSession.id == session_id))
    ).scalar_one_or_none()
    if row is not None:
        await repository.revoke_session(session, row)


# --- password reset -------------------------------------------------------


async def forgot_password(
    session: AsyncSession,
    *,
    email: str,
    ip: str | None = None,
) -> None:
    email_hash = hashlib.sha256(email.lower().encode()).hexdigest()
    await _enforce_rate_limit(
        f"forgot:email:{email_hash}", _FORGOT_EMAIL_LIMIT, "Too many reset requests"
    )
    if ip:
        await _enforce_rate_limit(f"forgot:ip:{ip}", _FORGOT_IP_LIMIT, "Too many reset requests")
    user = await repository.get_user_by_email(session, email)
    if user is None:
        await asyncio.sleep(_FORGOT_PASSWORD_MISS_DELAY_SECONDS)
        return
    raw_token = _new_token()
    token_row = await repository.create_password_reset_token(
        session,
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        expires_at=utc_now() + _RESET_TOKEN_TTL,
        requested_ip=ip,
    )
    # Enqueue the send; the job commits with the same transaction that wrote
    # the token row, so an email is queued iff the token persists.
    await queue.enqueue(
        session,
        queue="sync",
        type="email.send",
        payload={"kind": "reset", "token_id": str(token_row.id), "raw_token": raw_token},
        unique_key=f"reset:{token_row.id}",
    )


async def reset_password(session: AsyncSession, *, token: str, new_password: str) -> None:
    token_row = await repository.get_password_reset_token(session, _hash_token(token))
    if token_row is None or token_row.used_at is not None or token_row.expires_at < utc_now():
        raise ApiError("INVALID_TOKEN", "Reset link is invalid or expired", status_code=400)

    user = await repository.get_user_by_id(session, token_row.user_id)
    if user is None:
        raise ApiError("INVALID_TOKEN", "Reset link is invalid or expired", status_code=400)

    user.password_hash = await crypto.hash_password_async(new_password)
    await session.flush()
    await repository.mark_password_reset_token_used(session, token_row)
    # No "current" session to exempt — a token-based reset has no live
    # session of its own; every existing session for this user is revoked.
    await repository.revoke_all_sessions(session, user_id=user.id)


# --- invitations (owned here; features/team calls these through the barrel,
# never touching the `invitations` table directly) --------------------------


async def create_invitation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    organization_name: str,
    email: str,
    role: str,
    invited_by_user_id: uuid.UUID,
    invited_by_name: str,
    locale: str = "ar",
) -> Invitation:
    # Multi-membership is allowed (memberships unique on org+user), so an
    # existing Page7 account is fine — only reject when they already belong
    # to *this* organization.
    from app.features import team

    locale_norm = locale if locale in ("ar", "en") else "ar"

    existing_user = await repository.get_user_by_email(session, email)
    if existing_user is not None:
        membership = await team.get_membership(
            session, organization_id=organization_id, user_id=existing_user.id
        )
        if membership is not None:
            raise ApiError("EMAIL_IN_USE", "This person is already a member", status_code=409)
    if (
        await repository.get_pending_invitation_by_email(
            session, organization_id=organization_id, email=email
        )
        is not None
    ):
        raise ApiError(
            "EMAIL_IN_USE", "An invite is already pending for this email", status_code=409
        )

    raw_token = _new_token()
    invitation = await repository.create_invitation(
        session,
        organization_id=organization_id,
        email=email,
        role=role,
        token_hash=_hash_token(raw_token),
        invited_by=invited_by_user_id,
        expires_at=utc_now() + _INVITE_TTL,
    )
    # Enqueue the send inside the same transaction that created the invitation.
    await queue.enqueue(
        session,
        queue="sync",
        type="email.send",
        payload={
            "kind": "invite",
            "invitation_id": str(invitation.id),
            "organization_id": str(organization_id),
            "raw_token": raw_token,
            "inviter_name": invited_by_name,
            "locale": locale_norm,
        },
        unique_key=f"invite:{invitation.id}",
    )
    await audit.record(
        session,
        organization_id=organization_id,
        actor_kind="user",
        actor_ref=str(invited_by_user_id),
        actor_name=invited_by_name,
        action="team.invited",
        target_type="invitation",
        target_id=invitation.id,
    )
    return invitation


async def resend_invitation(
    session: AsyncSession,
    *,
    invitation: Invitation,
    organization_name: str,
    inviter_name: str,
    locale: str = "ar",
) -> Invitation:
    locale_norm = locale if locale in ("ar", "en") else "ar"
    raw_token = _new_token()
    resent_at = utc_now()
    await repository.mark_invitation_resent(
        session, invitation, token_hash=_hash_token(raw_token), expires_at=resent_at + _INVITE_TTL
    )
    # Stable idempotency key incorporates resent_at so re-resends don't collapse.
    await queue.enqueue(
        session,
        queue="sync",
        type="email.send",
        payload={
            "kind": "resend_invite",
            "invitation_id": str(invitation.id),
            "organization_id": str(invitation.organization_id),
            "raw_token": raw_token,
            "inviter_name": inviter_name,
            "locale": locale_norm,
            "resent_at": resent_at.isoformat(),
        },
        unique_key=f"invite-resend:{invitation.id}:{resent_at.isoformat()}",
    )
    return invitation


async def revoke_invitation(session: AsyncSession, *, invitation: Invitation) -> None:
    await repository.mark_invitation_revoked(session, invitation)


async def get_invitation_by_token(session: AsyncSession, *, token: str) -> Invitation | None:
    return await repository.get_invitation_by_token_hash(session, _hash_token(token))


async def get_invitation_by_id(
    session: AsyncSession, *, organization_id: uuid.UUID, invitation_id: uuid.UUID
) -> Invitation | None:
    return await repository.get_invitation_by_id(
        session, organization_id=organization_id, invitation_id=invitation_id
    )


async def list_pending_invitations(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> Sequence[Invitation]:
    return await repository.list_pending_invitations(session, organization_id=organization_id)


def _invitation_is_valid(invitation: Invitation) -> bool:
    return (
        invitation.accepted_at is None
        and invitation.revoked_at is None
        and invitation.expires_at >= utc_now()
    )


async def get_invite_preview(session: AsyncSession, *, token: str) -> Invitation:
    invitation = await get_invitation_by_token(session, token=token)
    if invitation is None or not _invitation_is_valid(invitation):
        raise ApiError("INVALID_TOKEN", "Invite is invalid or expired", status_code=400)
    return invitation


async def get_invite_preview_context(
    session: AsyncSession, *, invitation: Invitation
) -> tuple[Organization, str]:
    """What an invitee is allowed to see before authenticating — never the
    wider organization (auth.contract.ts's own comment on this shape).
    """
    from app.features import organizations

    org = await organizations.get_organization(session, invitation.organization_id)
    inviter = await repository.get_user_by_id(session, invitation.invited_by)
    inviter_name = inviter.name if inviter else ""
    return org, inviter_name


async def get_session_organization(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> Organization:
    from app.features import organizations

    try:
        return await organizations.get_organization(session, organization_id)
    except LookupError as exc:
        raise ApiError("UNAUTHORIZED", "Not authenticated", status_code=401) from exc


async def accept_invite(
    session: AsyncSession,
    *,
    token: str,
    name: str,
    password: str,
    locale: str = "en",
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuthResult:
    from app.features import organizations, team

    locale_norm = locale if locale in ("ar", "en") else "en"

    invitation = await get_invitation_by_token(session, token=token)
    if invitation is None or not _invitation_is_valid(invitation):
        raise ApiError("INVALID_TOKEN", "Invite is invalid or expired", status_code=400)

    existing = await repository.get_user_by_email(session, invitation.email)
    if existing is not None:
        already = await team.get_membership(
            session, organization_id=invitation.organization_id, user_id=existing.id
        )
        if already is not None:
            raise ApiError("EMAIL_IN_USE", "This person is already a member", status_code=409)
        # OAuth-only / MFA accounts can't complete this password-only form yet
        # without bypassing login's MFA gate or inventing a logged-in accept.
        if existing.password_hash is None:
            raise ApiError(
                "EMAIL_IN_USE",
                "This email already has an account that signs in with Google. "
                "Ask an admin to remove the invite and add you after you share a password account, "
                "or use a different email.",
                status_code=409,
            )
        # Same lockout buckets as login: invite preview already reveals the email.
        limiter = RateLimiter()
        now = utc_now()
        email_bucket = (
            f"login:email:{hashlib.sha256(invitation.email.lower().encode()).hexdigest()}"
        )
        email_count = await limiter.increment(
            bucket=email_bucket, now=now, window=_LOGIN_LOCKOUT_WINDOW
        )
        ip_count = 0
        if ip:
            ip_count = await limiter.increment(
                bucket=f"login:ip:{ip}", now=now, window=_LOGIN_LOCKOUT_WINDOW
            )
        if email_count > _LOGIN_LOCKOUT_THRESHOLD or ip_count > _LOGIN_LOCKOUT_THRESHOLD:
            raise ApiError("RATE_LIMIT", "Too many login attempts", status_code=429)
        if not await crypto.verify_password_async(existing.password_hash, password):
            raise ApiError("INVALID_CREDENTIALS", "Invalid email or password", status_code=401)
        # After password proof only — don't fingerprint MFA to the token holder first.
        if existing.mfa_enabled:
            raise ApiError(
                "MFA_REQUIRED",
                "This account has two-step verification. Password-only invite accept is not "
                "supported yet — use an email without MFA, or contact support.",
                status_code=403,
            )
        user = existing
    else:
        user = await repository.create_user(
            session,
            email=invitation.email,
            name=name,
            password_hash=await crypto.hash_password_async(password),
            locale=locale_norm,
            email_verified_at=utc_now(),
        )

    await team.create_membership(
        session,
        organization_id=invitation.organization_id,
        user_id=user.id,
        role=invitation.role,
        invited_by=invitation.invited_by,
    )
    await repository.mark_invitation_accepted(session, invitation, accepted_user_id=user.id)
    org = await organizations.get_organization(session, invitation.organization_id)
    await audit.record(
        session,
        organization_id=org.id,
        actor_kind="user",
        actor_ref=str(user.id),
        actor_name=user.name,
        action="team.invite_accepted",
        target_type="user",
        target_id=user.id,
    )
    return await _create_session_for(
        session,
        user=user,
        organization=org,
        role=invitation.role,
        remember_me=False,
        ip=ip,
        user_agent=user_agent,
    )


# --- MFA -------------------------------------------------------------------


def _totp_step_for(now: datetime | None = None) -> int:
    ts = time.time() if now is None else now.timestamp()
    return int(ts // 30)


def _find_matching_totp_step(secret: str, code: str, *, last_used_step: int | None) -> int | None:
    """±1-step drift tolerance, then reject a step at or before
    `last_used_step` even if the code would otherwise validate — a captured
    code replayed a few seconds later must not succeed (architecture/05 §2).
    """
    totp = pyotp.TOTP(secret)
    current_step = _totp_step_for()
    floor = -1 if last_used_step is None else last_used_step
    for step in (current_step - 1, current_step, current_step + 1):
        if step <= floor:
            continue
        if secrets.compare_digest(totp.generate_otp(step), code):
            return step
    return None


async def mfa_enroll(session: AsyncSession, *, user: User) -> MfaEnrollResult:
    secret = pyotp.random_base32()
    ciphertext, key_id = crypto.encrypt_secret(secret.encode())
    await repository.upsert_mfa_credential(
        session, user_id=user.id, totp_secret_enc=ciphertext, key_id=key_id
    )
    recovery_codes = [secrets.token_hex(4) for _ in range(_RECOVERY_CODE_COUNT)]
    await repository.replace_recovery_codes(
        session,
        user_id=user.id,
        code_hashes=[_hash_token(code) for code in recovery_codes],
    )
    otpauth_url = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Page7")
    return MfaEnrollResult(secret=secret, otpauth_url=otpauth_url, recovery_codes=recovery_codes)


async def mfa_verify_enrollment(session: AsyncSession, *, user: User, code: str) -> None:
    credential = await repository.get_mfa_credential(session, user.id)
    if credential is None:
        raise ApiError("INVALID_TOKEN", "No MFA enrollment in progress", status_code=400)
    secret = crypto.decrypt_secret(credential.totp_secret_enc, key_id=credential.key_id).decode()
    step = _find_matching_totp_step(secret, code, last_used_step=credential.last_used_step)
    if step is None:
        raise ApiError("INVALID_CREDENTIALS", "Invalid code", status_code=401)
    await repository.update_mfa_last_used_step(session, credential, step)
    await repository.mark_mfa_verified(session, credential)
    user.mfa_enabled = True
    await session.flush()


async def mfa_verify_challenge(
    session: AsyncSession,
    *,
    challenge_token: str,
    code: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuthResult:
    from app.features import organizations, team

    challenge = await repository.get_mfa_challenge_by_token_hash(
        session, _hash_token(challenge_token)
    )
    if challenge is None or challenge.consumed_at is not None or challenge.expires_at < utc_now():
        raise ApiError("INVALID_TOKEN", "Challenge is invalid or expired", status_code=400)

    limiter = RateLimiter()
    count = await limiter.increment(
        bucket=f"mfa:verify:{challenge.user_id}",
        now=utc_now(),
        window=_MFA_VERIFY_LOCKOUT_WINDOW,
    )
    if count > _MFA_VERIFY_LOCKOUT_THRESHOLD:
        raise ApiError("RATE_LIMIT", "Too many verification attempts", status_code=429)

    user = await repository.get_user_by_id(session, challenge.user_id)
    credential = await repository.get_mfa_credential(session, challenge.user_id)
    if user is None or credential is None:
        raise ApiError("INVALID_CREDENTIALS", "Invalid code", status_code=401)

    ok = await _consume_totp_or_recovery_code(session, user=user, credential=credential, code=code)
    if not ok:
        raise ApiError("INVALID_CREDENTIALS", "Invalid code", status_code=401)

    await repository.mark_mfa_challenge_consumed(session, challenge)
    membership = await team.get_default_membership(session, user.id)
    if membership is None:
        raise RuntimeError(f"user {user.id} has no organization")
    org = await organizations.get_organization(session, membership.organization_id)
    return await _create_session_for(
        session,
        user=user,
        organization=org,
        role=membership.role,
        remember_me=challenge.remember_me,
        ip=ip,
        user_agent=user_agent,
    )


async def _consume_totp_or_recovery_code(
    session: AsyncSession, *, user: User, credential: MfaCredential, code: str
) -> bool:
    secret = crypto.decrypt_secret(credential.totp_secret_enc, key_id=credential.key_id).decode()
    step = _find_matching_totp_step(secret, code, last_used_step=credential.last_used_step)
    if step is not None:
        await repository.update_mfa_last_used_step(session, credential, step)
        return True

    recovery = await repository.get_unused_recovery_code(
        session, user_id=user.id, code_hash=_hash_token(code)
    )
    if recovery is not None:
        await repository.mark_recovery_code_used(session, recovery)
        return True
    return False


# --- users/me ----------------------------------------------------------


async def get_users_by_ids(
    session: AsyncSession, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, User]:
    """Batches `features/team`'s team-list enrichment (member name/email/
    avatar) into one query rather than N+1 — team owns `memberships`, auth
    owns `users`, so the merge happens through this barrel call.
    """
    return await repository.get_users_by_ids(session, user_ids)


async def get_me(session: AsyncSession, *, user_id: uuid.UUID) -> User:
    user = await repository.get_user_by_id(session, user_id)
    if user is None:
        raise ApiError("UNAUTHORIZED", "Not authenticated", status_code=401)
    return user


async def update_me(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    name: str | None,
    name_ar: str | None,
    avatar_url: str | None,
) -> User:
    user = await get_me(session, user_id=user_id)
    if name is not None:
        user.name = name
    if name_ar is not None:
        user.name_ar = name_ar
    if avatar_url is not None:
        user.avatar_url = avatar_url
    await session.flush()
    return user


# --- oauth_states helpers (Phase 9; exported via auth barrel) --------------


@dataclass(frozen=True)
class OauthStateInfo:
    organization_id: uuid.UUID
    brand_id: uuid.UUID
    platform: str
    user_id: uuid.UUID
    session_id: uuid.UUID


async def create_oauth_state(
    session: AsyncSession,
    *,
    state_hash: str,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    platform: str,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    expires_at: datetime,
) -> None:
    await repository.insert_oauth_state(
        session,
        state_hash=state_hash,
        organization_id=organization_id,
        brand_id=brand_id,
        platform=platform,
        user_id=user_id,
        session_id=session_id,
        expires_at=expires_at,
    )


async def consume_oauth_state(
    session: AsyncSession,
    *,
    state_hash: str,
    session_id: uuid.UUID,
) -> OauthStateInfo | None:
    """Single-use consume. Returns None if missing, expired, consumed, or wrong session."""
    row = await repository.get_oauth_state(session, state_hash=state_hash)
    if row is None:
        return None
    if row.consumed_at is not None:
        return None
    if row.expires_at < utc_now():
        return None
    if row.session_id != session_id:
        return None
    await repository.mark_oauth_state_consumed(session, row)
    return OauthStateInfo(
        organization_id=row.organization_id,
        brand_id=row.brand_id,
        platform=row.platform,
        user_id=row.user_id,
        session_id=row.session_id,
    )


# --- Google OIDC -----------------------------------------------------------


def _auto_org_name(name: str, locale: str) -> str:
    first = name.strip().split()[0] if name.strip() else "My"
    if locale == "ar":
        return f"مساحة {first}"
    return f"{first}'s workspace"


def google_providers_enabled(*, settings: Settings) -> bool:
    return settings.auth.google_configured()


async def start_google_oauth(
    session: AsyncSession,
    *,
    settings: Settings,
    remember_me: bool,
    locale: str,
) -> GoogleStartResult:
    from app.features.auth import google as google_client

    if not settings.auth.google_configured():
        raise ApiError(
            "GOOGLE_AUTH_UNAVAILABLE", "Google sign-in is not configured", status_code=503
        )
    assert settings.auth.google_client_id is not None
    assert settings.auth.google_redirect_uri is not None

    locale_norm = locale if locale in ("ar", "en") else "en"
    raw_state = _new_token()
    nonce = _new_token()
    await repository.insert_auth_oauth_state(
        session,
        state_hash=_hash_token(raw_state),
        nonce=nonce,
        remember_me=remember_me,
        locale=locale_norm,
        expires_at=utc_now() + _AUTH_OAUTH_STATE_TTL,
    )
    url = google_client.build_authorize_url(
        client_id=settings.auth.google_client_id,
        redirect_uri=settings.auth.google_redirect_uri,
        state=raw_state,
        nonce=nonce,
    )
    return GoogleStartResult(authorize_url=url)


async def finish_google_oauth(
    session: AsyncSession,
    *,
    settings: Settings,
    code: str,
    state: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> GoogleFinishResult:
    from app.features import organizations, team
    from app.features.auth import google as google_client

    if not settings.auth.google_configured():
        raise ApiError(
            "GOOGLE_AUTH_UNAVAILABLE", "Google sign-in is not configured", status_code=503
        )

    state_row = await repository.get_auth_oauth_state(session, state_hash=_hash_token(state))
    if (
        state_row is None
        or state_row.consumed_at is not None
        or state_row.expires_at < utc_now()
    ):
        raise ApiError("INVALID_OAUTH_STATE", "OAuth state is invalid or expired", status_code=400)
    await repository.mark_auth_oauth_state_consumed(session, state_row)

    profile = await google_client.exchange_code(code, settings=settings.auth)

    identity = await repository.get_identity_by_provider_subject(
        session, provider="google", provider_subject=profile.subject
    )
    is_new_user = False
    user: User | None = None

    if identity is not None:
        user = await repository.get_user_by_id(session, identity.user_id)
    elif profile.email_verified:
        existing = await repository.get_user_by_email(session, profile.email)
        if existing is not None:
            user = existing
            await repository.create_identity(
                session,
                user_id=existing.id,
                provider="google",
                provider_subject=profile.subject,
                email=profile.email,
            )

    if user is None:
        if not profile.email_verified:
            raise ApiError(
                "GOOGLE_EMAIL_UNVERIFIED",
                "Google email must be verified to create an account",
                status_code=400,
            )
        user = await repository.create_user(
            session,
            email=profile.email,
            name=profile.name,
            password_hash=None,
            avatar_url=profile.picture,
            locale=state_row.locale,
            email_verified_at=utc_now(),
        )
        await repository.create_identity(
            session,
            user_id=user.id,
            provider="google",
            provider_subject=profile.subject,
            email=profile.email,
        )
        org = await organizations.create_organization_for_signup(
            session,
            name=_auto_org_name(profile.name, state_row.locale),
            created_by_user_id=user.id,
        )
        await team.create_membership(
            session, organization_id=org.id, user_id=user.id, role="owner"
        )
        await audit.record(
            session,
            organization_id=org.id,
            actor_kind="user",
            actor_ref=str(user.id),
            actor_name=user.name,
            action="org.created",
            target_type="organization",
            target_id=org.id,
        )
        is_new_user = True

    if user.deactivated_at is not None:
        raise ApiError("ACCOUNT_DISABLED", "Account is deactivated", status_code=403)

    user.last_login_at = utc_now()
    if profile.email_verified and user.email_verified_at is None:
        user.email_verified_at = utc_now()
    if profile.picture and user.avatar_url is None:
        user.avatar_url = profile.picture
    await session.flush()

    if user.mfa_enabled:
        raw_token = _new_token()
        await repository.create_mfa_challenge(
            session,
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=utc_now() + _MFA_CHALLENGE_TTL,
            remember_me=state_row.remember_me,
            ip=ip,
        )
        return GoogleFinishResult(
            locale=state_row.locale,
            is_new_user=is_new_user,
            mfa_challenge=MfaChallengeResult(raw_challenge_token=raw_token),
        )

    membership = await team.get_default_membership(session, user.id)
    if membership is None:
        raise RuntimeError(f"user {user.id} has no organization")
    org = await organizations.get_organization(session, membership.organization_id)
    auth_result = await _create_session_for(
        session,
        user=user,
        organization=org,
        role=membership.role,
        remember_me=state_row.remember_me,
        ip=ip,
        user_agent=user_agent,
    )
    return GoogleFinishResult(
        locale=state_row.locale,
        is_new_user=is_new_user,
        auth_result=auth_result,
    )

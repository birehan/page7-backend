from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.auth.models import (
    AuthOauthState,
    EmailVerificationChallenge,
    Invitation,
    MfaChallenge,
    MfaCredential,
    MfaRecoveryCode,
    OauthState,
    PasswordResetToken,
    User,
    UserIdentity,
)
from app.features.auth.models import Session as AuthSession

# --- users -------------------------------------------------------------


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    stmt = select(User).where(func.lower(User.email) == email.lower())
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_users_by_ids(
    session: AsyncSession, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, User]:
    if not user_ids:
        return {}
    stmt = select(User).where(User.id.in_(user_ids))
    users = (await session.execute(stmt)).scalars().all()
    return {user.id: user for user in users}


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    name: str,
    name_ar: str = "",
    password_hash: str | None = None,
    avatar_url: str | None = None,
    locale: str = "ar",
    email_verified_at: datetime | None = None,
) -> User:
    user = User(
        email=email,
        name=name,
        name_ar=name_ar,
        password_hash=password_hash,
        avatar_url=avatar_url,
        locale=locale,
        email_verified_at=email_verified_at,
    )
    session.add(user)
    await session.flush()
    return user


# --- sessions ------------------------------------------------------------


async def create_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
    remember_me: bool,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuthSession:
    row = AuthSession(
        user_id=user_id,
        organization_id=organization_id,
        token_hash=token_hash,
        expires_at=expires_at,
        remember_me=remember_me,
        ip=ip,
        user_agent=user_agent,
        mfa_verified_at=None,
    )
    session.add(row)
    await session.flush()
    return row


async def revoke_session(session: AsyncSession, row: AuthSession) -> None:
    row.revoked_at = utc_now()
    await session.flush()


async def revoke_other_sessions(
    session: AsyncSession, *, user_id: uuid.UUID, except_session_id: uuid.UUID
) -> None:
    """architecture/05 §1: a password change revokes every *other* live
    session — deliberately not the one that just proved it still knows the
    (new) password.
    """
    await session.execute(
        text(
            "UPDATE sessions SET revoked_at = now() WHERE user_id = :user_id "
            "AND id <> :except_session_id AND revoked_at IS NULL"
        ),
        {"user_id": user_id, "except_session_id": except_session_id},
    )


async def revoke_all_sessions(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    await session.execute(
        text(
            "UPDATE sessions SET revoked_at = now() WHERE user_id = :user_id AND revoked_at IS NULL"
        ),
        {"user_id": user_id},
    )


# --- password reset tokens ------------------------------------------------


async def create_password_reset_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
    requested_ip: str | None = None,
) -> PasswordResetToken:
    row = PasswordResetToken(
        user_id=user_id, token_hash=token_hash, expires_at=expires_at, requested_ip=requested_ip
    )
    session.add(row)
    await session.flush()
    return row


async def get_password_reset_token(
    session: AsyncSession, token_hash: str
) -> PasswordResetToken | None:
    stmt = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_password_reset_token_used(session: AsyncSession, row: PasswordResetToken) -> None:
    row.used_at = utc_now()
    await session.flush()


# --- invitations -----------------------------------------------------------


async def create_invitation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    email: str,
    role: str,
    token_hash: str,
    invited_by: uuid.UUID,
    expires_at: datetime,
) -> Invitation:
    row = Invitation(
        organization_id=organization_id,
        email=email,
        role=role,
        token_hash=token_hash,
        invited_by=invited_by,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def get_invitation_by_token_hash(session: AsyncSession, token_hash: str) -> Invitation | None:
    stmt = select(Invitation).where(Invitation.token_hash == token_hash)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_invitation_by_id(
    session: AsyncSession, *, organization_id: uuid.UUID, invitation_id: uuid.UUID
) -> Invitation | None:
    stmt = select(Invitation).where(
        Invitation.id == invitation_id, Invitation.organization_id == organization_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_pending_invitations(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> Sequence[Invitation]:
    stmt = select(Invitation).where(
        Invitation.organization_id == organization_id,
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
    )
    return (await session.execute(stmt)).scalars().all()


async def mark_invitation_accepted(
    session: AsyncSession, row: Invitation, *, accepted_user_id: uuid.UUID
) -> None:
    row.accepted_at = utc_now()
    row.accepted_user_id = accepted_user_id
    await session.flush()


async def mark_invitation_revoked(session: AsyncSession, row: Invitation) -> None:
    row.revoked_at = utc_now()
    await session.flush()


async def mark_invitation_resent(
    session: AsyncSession, row: Invitation, *, token_hash: str, expires_at: datetime
) -> None:
    row.resent_at = utc_now()
    row.token_hash = token_hash
    row.expires_at = expires_at
    await session.flush()


async def get_pending_invitation_by_email(
    session: AsyncSession, *, organization_id: uuid.UUID, email: str
) -> Invitation | None:
    stmt = select(Invitation).where(
        Invitation.organization_id == organization_id,
        func.lower(Invitation.email) == email.lower(),
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# --- MFA ---------------------------------------------------------------


async def get_mfa_credential(session: AsyncSession, user_id: uuid.UUID) -> MfaCredential | None:
    return await session.get(MfaCredential, user_id)


async def upsert_mfa_credential(
    session: AsyncSession, *, user_id: uuid.UUID, totp_secret_enc: bytes, key_id: str
) -> MfaCredential:
    existing = await get_mfa_credential(session, user_id)
    if existing is not None:
        existing.totp_secret_enc = totp_secret_enc
        existing.key_id = key_id
        existing.verified_at = None
        existing.last_used_step = None
        await session.flush()
        return existing
    row = MfaCredential(user_id=user_id, totp_secret_enc=totp_secret_enc, key_id=key_id)
    session.add(row)
    await session.flush()
    return row


async def mark_mfa_verified(session: AsyncSession, row: MfaCredential) -> None:
    row.verified_at = utc_now()
    await session.flush()


async def update_mfa_last_used_step(session: AsyncSession, row: MfaCredential, step: int) -> None:
    row.last_used_step = step
    await session.flush()


async def replace_recovery_codes(
    session: AsyncSession, *, user_id: uuid.UUID, code_hashes: list[str]
) -> None:
    await session.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id))
    for code_hash in code_hashes:
        session.add(MfaRecoveryCode(user_id=user_id, code_hash=code_hash))
    await session.flush()


async def get_unused_recovery_code(
    session: AsyncSession, *, user_id: uuid.UUID, code_hash: str
) -> MfaRecoveryCode | None:
    stmt = select(MfaRecoveryCode).where(
        MfaRecoveryCode.user_id == user_id,
        MfaRecoveryCode.code_hash == code_hash,
        MfaRecoveryCode.used_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_recovery_code_used(session: AsyncSession, row: MfaRecoveryCode) -> None:
    row.used_at = utc_now()
    await session.flush()


async def create_mfa_challenge(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
    remember_me: bool,
    ip: str | None = None,
) -> MfaChallenge:
    row = MfaChallenge(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
        remember_me=remember_me,
        ip=ip,
    )
    session.add(row)
    await session.flush()
    return row


async def get_mfa_challenge_by_token_hash(
    session: AsyncSession, token_hash: str
) -> MfaChallenge | None:
    stmt = select(MfaChallenge).where(MfaChallenge.token_hash == token_hash)
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_mfa_challenge_consumed(session: AsyncSession, row: MfaChallenge) -> None:
    row.consumed_at = utc_now()
    await session.flush()


# --- email verification challenges ----------------------------------------


async def consume_active_email_verification_challenges(
    session: AsyncSession, *, user_id: uuid.UUID
) -> None:
    stmt = select(EmailVerificationChallenge).where(
        EmailVerificationChallenge.user_id == user_id,
        EmailVerificationChallenge.consumed_at.is_(None),
    )
    rows = (await session.execute(stmt)).scalars().all()
    now = utc_now()
    for row in rows:
        row.consumed_at = now
    if rows:
        await session.flush()


async def create_email_verification_challenge(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    token_hash: str,
    code_salt: str,
    code_hash: str,
    expires_at: datetime,
    locale: str,
    ip: str | None = None,
) -> EmailVerificationChallenge:
    row = EmailVerificationChallenge(
        user_id=user_id,
        token_hash=token_hash,
        code_salt=code_salt,
        code_hash=code_hash,
        expires_at=expires_at,
        locale=locale,
        ip=ip,
    )
    session.add(row)
    await session.flush()
    return row


async def get_email_verification_challenge_by_token_hash(
    session: AsyncSession, token_hash: str
) -> EmailVerificationChallenge | None:
    stmt = select(EmailVerificationChallenge).where(
        EmailVerificationChallenge.token_hash == token_hash
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_email_verification_challenge_consumed(
    session: AsyncSession, row: EmailVerificationChallenge
) -> None:
    row.consumed_at = utc_now()
    await session.flush()


async def record_email_verification_failure(
    challenge_id: uuid.UUID, *, max_attempts: int
) -> int:
    """Increment attempt_count in its own transaction (survives ApiError rollback).

    Returns the new attempt_count. Consumes the challenge once attempts hit
    ``max_attempts`` so the code cannot be reused.
    """
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        row = await session.get(EmailVerificationChallenge, challenge_id)
        if row is None:
            return max_attempts
        row.attempt_count += 1
        if row.attempt_count >= max_attempts:
            row.consumed_at = utc_now()
        count = row.attempt_count
        await session.commit()
        return count


# --- oauth_states (Phase 9 Zernio connect) ---------------------------------


async def insert_oauth_state(
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
    session.add(
        OauthState(
            state_hash=state_hash,
            organization_id=organization_id,
            brand_id=brand_id,
            platform=platform,
            user_id=user_id,
            session_id=session_id,
            expires_at=expires_at,
        )
    )
    await session.flush()


async def get_oauth_state(
    session: AsyncSession, *, state_hash: str
) -> OauthState | None:
    return await session.get(OauthState, state_hash)


async def mark_oauth_state_consumed(session: AsyncSession, row: OauthState) -> None:
    row.consumed_at = utc_now()
    await session.flush()


# --- Google OIDC identities / auth oauth states -----------------------------


async def get_identity_by_provider_subject(
    session: AsyncSession, *, provider: str, provider_subject: str
) -> UserIdentity | None:
    stmt = select(UserIdentity).where(
        UserIdentity.provider == provider,
        UserIdentity.provider_subject == provider_subject,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_identity(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    provider: str,
    provider_subject: str,
    email: str | None = None,
) -> UserIdentity:
    row = UserIdentity(
        user_id=user_id,
        provider=provider,
        provider_subject=provider_subject,
        email=email,
    )
    session.add(row)
    await session.flush()
    return row


async def insert_auth_oauth_state(
    session: AsyncSession,
    *,
    state_hash: str,
    nonce: str,
    remember_me: bool,
    locale: str,
    expires_at: datetime,
) -> None:
    session.add(
        AuthOauthState(
            state_hash=state_hash,
            nonce=nonce,
            remember_me=remember_me,
            locale=locale,
            expires_at=expires_at,
        )
    )
    await session.flush()


async def get_auth_oauth_state(
    session: AsyncSession, *, state_hash: str
) -> AuthOauthState | None:
    return await session.get(AuthOauthState, state_hash)


async def mark_auth_oauth_state_consumed(
    session: AsyncSession, row: AuthOauthState
) -> None:
    row.consumed_at = utc_now()
    await session.flush()

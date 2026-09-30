"""architecture/03 'Migration tests' #3: one row per new table, supplying only
its NOT-NULL-without-default columns, catches a missing server default before
it reaches a real deploy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.features.audit.models import AuditLog
from app.features.auth.models import (
    Invitation,
    MfaChallenge,
    MfaCredential,
    MfaRecoveryCode,
    OauthState,
    PasswordResetToken,
    User,
)
from app.features.auth.models import Session as AuthSession
from app.features.notifications.models import Notification, NotificationRecipient
from app.features.organizations.models import Organization, OrgSettings
from app.features.team.models import Membership


async def test_users_defaults(db_session: AsyncSession) -> None:
    user = User(email="defaults-user@example.com", name="Defaults User")
    db_session.add(user)
    await db_session.flush()

    assert user.name_ar == ""
    assert user.locale == "ar"
    assert user.mfa_enabled is False


async def test_organizations_defaults(db_session: AsyncSession) -> None:
    org = Organization(name="Defaults Org")
    db_session.add(org)
    await db_session.flush()

    assert org.industry == ""
    assert org.city == ""
    assert org.timezone == "Asia/Riyadh"


async def test_org_settings_defaults(db_session: AsyncSession) -> None:
    org = Organization(name="Settings Org")
    db_session.add(org)
    await db_session.flush()

    settings = OrgSettings(organization_id=org.id)
    db_session.add(settings)
    await db_session.flush()

    assert settings.publishing_frozen is False
    assert settings.approval_mode == "always"
    assert settings.risk_threshold == Decimal("0.150")
    assert settings.notification_prefs == {}
    assert settings.onboarding == {
        "brand": False,
        "channel": False,
        "plan": False,
        "firstApproval": False,
    }


async def test_memberships_defaults(db_session: AsyncSession) -> None:
    user = User(email="member-defaults@example.com", name="Member")
    org = Organization(name="Membership Org")
    db_session.add_all([user, org])
    await db_session.flush()

    membership = Membership(organization_id=org.id, user_id=user.id, role="owner")
    db_session.add(membership)
    await db_session.flush()

    assert membership.joined_at is not None


async def test_sessions_and_oauth_states_defaults(db_session: AsyncSession) -> None:
    from app.features.brands.models import Brand

    user = User(email="session-defaults@example.com", name="Session User")
    org = Organization(name="Session Org")
    db_session.add_all([user, org])
    await db_session.flush()

    brand = Brand(
        organization_id=org.id,
        name="Session Brand",
        industry="retail",
        city="Riyadh",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()

    session = AuthSession(
        user_id=user.id,
        organization_id=org.id,
        token_hash="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=12),
    )
    db_session.add(session)
    await db_session.flush()

    assert session.remember_me is False
    assert session.last_seen_at is not None

    oauth_state = OauthState(
        state_hash="b" * 64,
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
        user_id=user.id,
        session_id=session.id,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db_session.add(oauth_state)
    await db_session.flush()

    assert oauth_state.consumed_at is None


async def test_invitations_and_audit_logs_and_notifications_defaults(
    db_session: AsyncSession,
) -> None:
    user = User(email="inviter@example.com", name="Inviter")
    org = Organization(name="Invite Org")
    db_session.add_all([user, org])
    await db_session.flush()

    invitation = Invitation(
        organization_id=org.id,
        email="invitee@example.com",
        role="editor",
        token_hash="c" * 64,
        invited_by=user.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    audit_log = AuditLog(
        organization_id=org.id,
        actor_kind="user",
        actor_ref=str(user.id),
        actor_name=user.name,
        action="org.created",
        target_type="organization",
        target_id=org.id,
    )
    notification = Notification(
        organization_id=org.id,
        type="mention",
        message_key="notifications.mention",
        target_href="/posts/1",
    )
    db_session.add_all([invitation, audit_log, notification])
    await db_session.flush()

    assert invitation.revoked_at is None
    assert audit_log.meta is None
    assert notification.params == {}

    recipient = NotificationRecipient(notification_id=notification.id, user_id=user.id)
    db_session.add(recipient)
    await db_session.flush()
    assert recipient.read_at is None


async def test_mfa_tables_accept_their_required_columns(db_session: AsyncSession) -> None:
    user = User(email="mfa-defaults@example.com", name="MFA User")
    db_session.add(user)
    await db_session.flush()

    db_session.add(MfaCredential(user_id=user.id, totp_secret_enc=b"ciphertext", key_id="k1"))
    db_session.add(MfaRecoveryCode(user_id=user.id, code_hash="d" * 64))
    db_session.add(
        MfaChallenge(
            user_id=user.id,
            token_hash="e" * 64,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            remember_me=False,
        )
    )
    db_session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash="f" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()

"""Server-default smoke tests for Phase 12 analytics + inbox tables."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.analytics.models import (
    AnalyticsSyncState,
    InsightReport,
    MetricSnapshot,
)
from app.features.auth.models import User
from app.features.brands.models import Brand
from app.features.inbox.models import Conversation, ConversationMessage, SavedReply
from app.features.organizations.models import Organization
from app.features.posts.models import Post
from app.features.social_accounts.models import (
    SocialAccount,
    ZernioCredential,
    ZernioProfile,
)


async def _seed(
    db_session: AsyncSession,
) -> tuple[Organization, Brand, User, Post, SocialAccount, ZernioCredential]:
    user = User(email="analytics-inbox-defaults@example.com", name="AI User")
    org = Organization(name="AI Org")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="AI Brand",
        industry="retail",
        city="Riyadh",
        website="https://example.sa",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()

    cred = (
        await db_session.execute(
            select(ZernioCredential).where(ZernioCredential.alias == "t1")
        )
    ).scalar_one()
    profile = ZernioProfile(
        organization_id=org.id,
        brand_id=brand.id,
        credential_id=cred.id,
        zernio_profile_id="zp_ai_defaults",
    )
    db_session.add(profile)
    await db_session.flush()

    account = SocialAccount(
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
        zernio_profile_id=profile.id,
        credential_id=cred.id,
        zernio_account_id="za_ai_defaults",
        status="connected",
    )
    db_session.add(account)
    await db_session.flush()

    post = Post(
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
        status="draft",
        scheduled_at=datetime.now(UTC),
        created_by=user.id,
        variants=[{"lang": "en", "caption": "hello"}],
    )
    db_session.add(post)
    await db_session.flush()
    return org, brand, user, post, account, cred


async def test_metric_snapshots_defaults(db_session: AsyncSession) -> None:
    org, brand, _user, post, account, _cred = await _seed(db_session)
    row = MetricSnapshot(
        organization_id=org.id,
        brand_id=brand.id,
        social_account_id=account.id,
        platform="instagram",
        post_id=post.id,
        metric_date=date(2026, 9, 1),
        impressions=100,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.id is not None
    assert row.captured_at is not None


async def test_metric_snapshots_platform_check(db_session: AsyncSession) -> None:
    org, brand, _user, _post, account, _cred = await _seed(db_session)
    row = MetricSnapshot(
        organization_id=org.id,
        brand_id=brand.id,
        social_account_id=account.id,
        platform="linkedin",
        metric_date=date(2026, 9, 1),
    )
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_insight_reports_defaults_and_unique(
    db_session: AsyncSession,
) -> None:
    org, brand, _user, _post, _account, _cred = await _seed(db_session)
    row = InsightReport(
        organization_id=org.id,
        brand_id=brand.id,
        week_of=date(2026, 9, 1),
        what_happened="grew",
        why="posts",
        what_to_change="more",
        metrics={},
        series={},
        pillar_breakdown={},
        platform_breakdown={},
        generated_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.flush()
    assert row.id is not None
    assert row.created_at is not None
    assert row.next_actions == []

    dup = InsightReport(
        organization_id=org.id,
        brand_id=brand.id,
        week_of=date(2026, 9, 1),
        what_happened="again",
        why="x",
        what_to_change="y",
        metrics={},
        series={},
        pillar_breakdown={},
        platform_breakdown={},
        generated_at=datetime.now(UTC),
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def _ephemeral_credential(
    db_session: AsyncSession, *, alias: str
) -> ZernioCredential:
    """Dedicated credential so sync_state PK tests don't collide with shared t1/t2/t3."""
    cred = ZernioCredential(
        alias=alias,
        secret_ref=f"ZERNIO_API_KEY__{alias}",
        max_profiles=3,
        status="active",
    )
    db_session.add(cred)
    await db_session.flush()
    return cred


async def test_analytics_sync_state_defaults(db_session: AsyncSession) -> None:
    cred = await _ephemeral_credential(db_session, alias="sync_defaults_test")
    row = AnalyticsSyncState(credential_id=cred.id)
    db_session.add(row)
    await db_session.flush()
    assert row.last_sync_status == "ok"
    assert row.consecutive_failures == 0
    assert row.updated_at is not None
    assert row.last_cursor is None


async def test_analytics_sync_state_status_check(db_session: AsyncSession) -> None:
    cred = await _ephemeral_credential(db_session, alias="sync_status_check_test")
    row = AnalyticsSyncState(credential_id=cred.id, last_sync_status="bogus")
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_conversations_defaults(db_session: AsyncSession) -> None:
    org, brand, _user, _post, account, _cred = await _seed(db_session)
    row = Conversation(
        organization_id=org.id,
        brand_id=brand.id,
        social_account_id=account.id,
        platform="instagram",
        kind="dm",
        external_thread_id="thread_1",
        participant_name="Fatima",
        last_message_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.flush()
    assert row.id is not None
    assert row.status == "open"
    assert row.sentiment == "neutral"
    assert row.escalated is False
    assert row.suggested_reply_ar == ""
    assert row.suggested_reply_en == ""
    assert row.message_count == 0
    assert row.tags == []


async def test_conversations_kind_check(db_session: AsyncSession) -> None:
    org, brand, _user, _post, account, _cred = await _seed(db_session)
    row = Conversation(
        organization_id=org.id,
        brand_id=brand.id,
        social_account_id=account.id,
        platform="instagram",
        kind="story",
        external_thread_id="thread_bad",
        participant_name="x",
        last_message_at=datetime.now(UTC),
    )
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_conversation_messages_checks(db_session: AsyncSession) -> None:
    org, brand, _user, _post, account, _cred = await _seed(db_session)
    conv = Conversation(
        organization_id=org.id,
        brand_id=brand.id,
        social_account_id=account.id,
        platform="instagram",
        kind="comment",
        external_thread_id="thread_msg",
        participant_name="Ali",
        last_message_at=datetime.now(UTC),
    )
    db_session.add(conv)
    await db_session.flush()

    msg = ConversationMessage(
        organization_id=org.id,
        conversation_id=conv.id,
        direction="inbound",
        body="مرحبا",
        lang="ar",
        author_name="Ali",
        delivery_status="received",
    )
    db_session.add(msg)
    await db_session.flush()
    assert msg.id is not None
    assert msg.created_at is not None

    bad = ConversationMessage(
        organization_id=org.id,
        conversation_id=conv.id,
        direction="sideways",
        body="nope",
        lang="ar",
        author_name="Ali",
        delivery_status="received",
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_saved_replies_defaults_and_title_unique(
    db_session: AsyncSession,
) -> None:
    org, _brand, user, _post, _account, _cred = await _seed(db_session)
    row = SavedReply(
        organization_id=org.id,
        title="Thanks",
        body_ar="شكرا",
        body_en="Thanks",
        created_by=user.id,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.id is not None
    assert row.usage_count == 0
    assert row.deleted_at is None

    dup = SavedReply(
        organization_id=org.id,
        title="thanks",
        body_ar="x",
        body_en="y",
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.flush()

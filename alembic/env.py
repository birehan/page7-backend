from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings
from app.db.base import Base

# Import every model module so Base.metadata is fully populated before
# autogenerate compares it against the live database. Add each new feature's
# models module here as it's created.
from app.features.analytics import models as _analytics_models  # noqa: F401
from app.features.audit import models as _audit_models  # noqa: F401
from app.features.auth import models as _auth_models  # noqa: F401
from app.features.billing import models as _billing_models  # noqa: F401
from app.features.brand_research import models as _brand_research_models  # noqa: F401
from app.features.brands import models as _brands_models  # noqa: F401
from app.features.content_ai import models as _content_ai_models  # noqa: F401
from app.features.cultural_events import models as _cultural_events_models  # noqa: F401
from app.features.inbox import models as _inbox_models  # noqa: F401
from app.features.media import models as _media_models  # noqa: F401
from app.features.notifications import models as _notifications_models  # noqa: F401
from app.features.organizations import models as _organizations_models  # noqa: F401
from app.features.posts import models as _posts_models  # noqa: F401
from app.features.publishing import models as _publishing_models  # noqa: F401
from app.features.review_links import models as _review_links_models  # noqa: F401
from app.features.social_accounts import models as _social_accounts_models  # noqa: F401
from app.features.strategy import models as _strategy_models  # noqa: F401
from app.features.team import models as _team_models  # noqa: F401
from app.features.visuals import models as _visuals_models  # noqa: F401
from app.features.waitlist import models as _waitlist_models  # noqa: F401
from app.infrastructure.idempotency import models as _idempotency_models  # noqa: F401
from app.infrastructure.ratelimit import models as _ratelimit_models  # noqa: F401
from app.jobs import models as _jobs_models  # noqa: F401
from app.sse import models as _sse_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ConfigParser treats '%' as interpolation; passwords in URLs often contain
# percent-encoded bytes (%23 for '#'). Escape so set_main_option accepts them.
config.set_main_option(
    "sqlalchemy.url",
    get_settings().database.url.replace("%", "%%"),
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # architecture/03 — every migration session sets lock_timeout before DDL.
        connect_args={"server_settings": {"lock_timeout": "5s"}},
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

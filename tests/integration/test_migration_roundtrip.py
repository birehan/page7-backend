from __future__ import annotations

from alembic.config import Config

from alembic import command

# Forward-only data migrations raise NotImplementedError on downgrade (0011, 0017).
# Schema revisions after those must be downgraded for real before we stamp past the
# data migration — otherwise leftover tables (e.g. inbox_sync_state from 0018) keep
# FKs on social_accounts and break the 0010→base drop.
_HEAD_OR_POST_SEED = "0017_seed_billing"
_PRE_SEED_BILLING = "0016_billing"
_PRE_DATA_MIGRATION_REVISION = "0010_social_accounts_zernio"
_DATA_MIGRATION_REVISION = "0011_backfill_social_accts"


def test_upgrade_downgrade_upgrade_round_trips_cleanly() -> None:
    """Definition of Done: alembic upgrade head -> downgrade base -> upgrade head
    again, plus alembic check, all clean.

    Runs against ``pgblank_test`` only (see ``tests/conftest.py`` / ``just test``).
    Never against the live ``pgblank`` acceptance database on the compose volume.
    """
    cfg = Config("alembic.ini")
    # Drop 0020/0019/0018 schema first (lands on forward-only 0017).
    command.downgrade(cfg, _HEAD_OR_POST_SEED)
    command.stamp(cfg, _PRE_SEED_BILLING)
    command.downgrade(cfg, _DATA_MIGRATION_REVISION)
    command.stamp(cfg, _PRE_DATA_MIGRATION_REVISION)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    command.check(cfg)

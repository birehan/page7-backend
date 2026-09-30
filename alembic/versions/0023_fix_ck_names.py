"""Fix double-qualified check-constraint names on 0022's two new tables.

Revision ID: 0023_fix_ck_names
Revises: 0022_google_auth_identities
Create Date: 2026-09-27

`0022_google_auth_identities` created its two CheckConstraints with an
already-fully-qualified `name=` string (e.g. `ck_user_identities_provider_valid`)
instead of wrapping it in `op.f(...)` the way every other constraint in that
same migration correctly does. `app/db/base.py`'s naming convention
(`"ck": "ck_%(table_name)s_%(constraint_name)s"`) re-applies its own template
on top of an un-wrapped name at DDL-execution time, so the constraint that
actually landed on a real database is doubled:
`ck_auth_oauth_states_ck_auth_oauth_states_locale_valid` and
`ck_user_identities_ck_user_identities_provider_valid` — not the clean names
the ORM model in `app/features/auth/models.py` (correctly written with a bare,
short constraint name and left to the naming convention) expects. This is
invisible in normal `upgrade head` usage (Postgres doesn't care what a
constraint is named), but it makes `alembic downgrade base` -> `upgrade head`
a moving target: autogenerate sees the model's expected `ck_..._locale_valid`
and the database's actual `ck_..._ck_..._locale_valid` as two different
constraints, which is exactly what
`tests/integration/test_migration_roundtrip.py` caught.

This migration renames both constraints, in place, to the names the ORM
model / naming convention actually expect — it does not change 0022 itself
(never edit an already-applied migration), it only corrects what 0022 left
behind.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023_fix_ck_names"
down_revision: str | None = "0022_google_auth_identities"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_RENAMES = [
    (
        "user_identities",
        "ck_user_identities_ck_user_identities_provider_valid",
        "ck_user_identities_provider_valid",
    ),
    (
        "auth_oauth_states",
        "ck_auth_oauth_states_ck_auth_oauth_states_locale_valid",
        "ck_auth_oauth_states_locale_valid",
    ),
]


def upgrade() -> None:
    for table, old_name, new_name in _RENAMES:
        op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old_name}" TO "{new_name}"')


def downgrade() -> None:
    for table, old_name, new_name in _RENAMES:
        op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{new_name}" TO "{old_name}"')

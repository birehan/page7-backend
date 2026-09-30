"""Allow rasterized templates to be kind=image with source=template.

Revision ID: 0019_template_source_params
Revises: 0018_inbox_sync_state
Create Date: 2026-09-05

Rasterized template PNGs are real publishable images. Provenance lives on
`source='template'` + `template_params`, not on `kind='template'`. Relaxes the
CHECK so template_params is required iff source is template.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_template_source_params"
down_revision: str | None = "0018_inbox_sync_state"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "template_params_iff_template", "media_assets", type_="check"
    )
    op.create_check_constraint(
        "template_params_iff_template",
        "media_assets",
        "(source = 'template') = (template_params IS NOT NULL)",
    )


def downgrade() -> None:
    # New rows use kind=image + source=template; old CHECK keyed off kind.
    op.execute(
        "UPDATE media_assets SET kind = 'template' "
        "WHERE source = 'template' AND template_params IS NOT NULL"
    )
    op.drop_constraint(
        "template_params_iff_template", "media_assets", type_="check"
    )
    op.create_check_constraint(
        "template_params_iff_template",
        "media_assets",
        "(kind = 'template') = (template_params IS NOT NULL)",
    )

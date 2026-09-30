"""Add poster style for Qwen Image 3 text-bearing visuals.

Revision ID: 0020_poster_style
Revises: 0019_template_source_params
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_poster_style"
down_revision: str | None = "0019_template_source_params"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("style_valid", "image_generations", type_="check")
    op.create_check_constraint(
        "style_valid",
        "image_generations",
        "style IN ('photo', 'flat', 'three-d', 'minimal', 'saudi-modern', 'poster')",
    )


def downgrade() -> None:
    # Rows written while `poster` was allowed must move before the tighter CHECK.
    op.execute(
        "UPDATE image_generations SET style = 'photo' WHERE style = 'poster'"
    )
    op.drop_constraint("style_valid", "image_generations", type_="check")
    op.create_check_constraint(
        "style_valid",
        "image_generations",
        "style IN ('photo', 'flat', 'three-d', 'minimal', 'saudi-modern')",
    )

"""Счётчик попыток

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("tasks", "attempts", server_default=None)


def downgrade() -> None:
    op.drop_column("tasks", "attempts")

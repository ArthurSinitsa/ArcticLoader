"""Профиль качества, прямые ссылки и кэш метаданных

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_QUALITY = "1080p"


def upgrade() -> None:
    # server_default нужен только чтобы заполнить существующие строки —
    # дальше значение всегда приходит из приложения.
    op.add_column(
        "tasks",
        sa.Column("quality", sa.String(16), nullable=False, server_default=DEFAULT_QUALITY),
    )
    op.alter_column("tasks", "quality", server_default=None)
    op.add_column("tasks", sa.Column("direct_url", sa.Text(), nullable=True))

    op.create_table(
        "url_meta",
        sa.Column("url_hash", sa.String(64), primary_key=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("extractor", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_url_meta_fetched_at", "url_meta", ["fetched_at"])


def downgrade() -> None:
    op.drop_table("url_meta")
    op.drop_column("tasks", "direct_url")
    op.drop_column("tasks", "quality")

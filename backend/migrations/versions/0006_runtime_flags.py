"""Глобальные рубильники

Строки заводятся при первом переключении: отсутствие записи означает значение
по умолчанию из `app.services.flags.DEFAULTS`. Поэтому таблица создаётся
пустой — заполнять её нечем и незачем.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_flags",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("runtime_flags")

"""Заявки на регистрацию и срок временного пароля

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.models import request_status_enum

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Тип создаётся самим `create_table` — как в миграции 0001. Отдельный
    # `create()` рядом с ним выпустил бы CREATE TYPE второй раз и упал.
    op.create_table(
        "registration_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("telegram_username", sa.String(64), nullable=True),
        sa.Column("email", sa.String(255), nullable=False, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("status", request_status_enum, nullable=False, index=True),
        # SET NULL: уход админа не должен стирать историю решений по заявкам.
        sa.Column(
            "processed_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    # Пусто у существующих учёток: их пароли уже боевые, а не временные.
    op.add_column(
        "users",
        sa.Column("password_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "password_expires_at")
    op.drop_table("registration_requests")
    request_status_enum.drop(op.get_bind(), checkfirst=True)

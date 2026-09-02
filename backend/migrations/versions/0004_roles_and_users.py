"""Роли, квоты и пользователи

Только структура: справочник ролей заполняет приложение на старте
(`app.services.roles.ensure_roles`). Держать цифры лимитов ещё и здесь значило
бы иметь два источника истины, которые разъедутся после первой правки квот
из админки.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(16), nullable=False, unique=True),
        sa.Column("title", sa.String(64), nullable=False),
        sa.Column("is_unlimited", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "quota_profiles",
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("concurrent_limit", sa.Integer(), nullable=False),
        sa.Column("bucket_capacity", sa.Integer(), nullable=False),
        sa.Column("bucket_refill_minutes", sa.Integer(), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Задачи существовали до пользователей: колонка уже есть, ключа не было.
    # SET NULL, а не CASCADE: удаление учётки не должно стирать историю
    # загрузок — она нужна журналу (раздел 3.6).
    op.create_foreign_key(
        "tasks_user_id_fkey", "tasks", "users", ["user_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("tasks_user_id_fkey", "tasks", type_="foreignkey")
    op.drop_table("users")
    op.drop_table("quota_profiles")
    op.drop_table("roles")

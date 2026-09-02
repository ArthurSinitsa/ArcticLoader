"""Доступ в раздел администрирования — матрица 2.5.1.

Главное правило матрицы: админ не повышает роли и не трогает других админов.
Иначе разделение уровней теряет смысл — любой админ становится суперадмином
за два клика.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.roles import RoleCode
from tests.test_auth import add_user, login

ADMIN = "admin@example.com"
SUPER = "super@example.com"


async def test_guest_sees_no_admin_section(client: AsyncClient) -> None:
    assert (await client.get("/api/admin/users")).status_code == 401


async def test_plain_user_is_refused(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory)
    await login(client)

    assert (await client.get("/api/admin/users")).status_code == 403


async def test_admin_is_allowed(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN)
    await login(client, email=ADMIN)

    assert (await client.get("/api/admin/users")).status_code == 200


async def test_superadmin_is_allowed(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory, email=SUPER, role=RoleCode.SUPERADMIN)
    await login(client, email=SUPER)

    assert (await client.get("/api/admin/users")).status_code == 200


async def test_quota_editing_is_superadmin_only(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Правка квот — за суперадмином: иначе админ поднимает лимиты себе."""
    await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN)
    await login(client, email=ADMIN)

    response = await client.put("/api/admin/quotas/user", json={"daily_limit": 999})

    assert response.status_code == 403


async def test_journal_is_superadmin_only(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Журнал видит тот, кто не может его подчистить своими же действиями."""
    await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN)
    await login(client, email=ADMIN)

    assert (await client.get("/api/admin/audit")).status_code == 403


async def test_user_with_temporary_password_gets_nothing(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Временный пароль лежит в чужих руках — админка тем более закрыта."""
    await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN, must_change_password=True)
    await login(client, email=ADMIN)

    assert (await client.get("/api/admin/users")).status_code == 403

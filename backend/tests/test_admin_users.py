"""Управление учётными записями — матрица 2.5.1."""

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditEntry, User
from app.security import verify_password
from app.services.roles import RoleCode
from tests.test_auth import add_user, login

ADMIN = "admin@example.com"
SUPER = "super@example.com"


async def sign_in_admin(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    role: RoleCode = RoleCode.ADMIN,
    email: str = ADMIN,
) -> User:
    user = await add_user(session_factory, email=email, role=role)
    await login(client, email=email)
    return user


async def test_list_shows_accounts(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in_admin(client, session_factory)
    await add_user(session_factory, email="кто-то@example.com")

    body = (await client.get("/api/admin/users")).json()

    assert body["total"] == 2
    assert {item["email"] for item in body["items"]} == {ADMIN, "кто-то@example.com"}


async def test_search_finds_by_email(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in_admin(client, session_factory)
    await add_user(session_factory, email="иванов@example.com")

    body = (await client.get("/api/admin/users?search=иванов")).json()

    assert body["total"] == 1
    assert body["items"][0]["email"] == "иванов@example.com"


async def test_search_is_case_insensitive(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Админ ищет по памяти, а не по точному написанию."""
    await sign_in_admin(client, session_factory)
    await add_user(session_factory, email="Petrov@Example.com")

    body = (await client.get("/api/admin/users?search=petrov")).json()

    assert body["total"] == 1


async def test_filter_by_role(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in_admin(client, session_factory)
    await add_user(session_factory)

    body = (await client.get(f"/api/admin/users?role={RoleCode.ADMIN}")).json()

    assert body["total"] == 1
    assert body["items"][0]["role"] == RoleCode.ADMIN


async def test_filter_by_activity(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in_admin(client, session_factory)
    await add_user(session_factory, email="выключен@example.com", is_active=False)

    body = (await client.get("/api/admin/users?is_active=false")).json()

    assert body["total"] == 1
    assert body["items"][0]["email"] == "выключен@example.com"


async def test_sorting_by_email(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in_admin(client, session_factory)
    await add_user(session_factory, email="aaa@example.com")

    body = (await client.get("/api/admin/users?sort=email&order=asc")).json()

    assert body["items"][0]["email"] == "aaa@example.com"


async def test_created_account_gets_a_temporary_password(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Пароль показывается один раз — его сообщают человеку, а не хранят."""
    await sign_in_admin(client, session_factory)

    response = await client.post(
        "/api/admin/users", json={"email": "новый@example.com", "name": "Новый"}
    )

    body = response.json()
    assert response.status_code == 201
    assert body["temporary_password"]
    assert body["user"]["must_change_password"] is True

    async with session_factory() as session:
        created = await session.scalar(sa.select(User).where(User.email == "новый@example.com"))
    assert verify_password(body["temporary_password"], created.password_hash)


async def test_duplicate_email_is_refused(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in_admin(client, session_factory)

    response = await client.post("/api/admin/users", json={"email": ADMIN, "name": "Двойник"})

    assert response.status_code == 409


async def test_admin_cannot_create_another_admin(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Иначе админ заводит себе второго и обходит любые ограничения роли."""
    await sign_in_admin(client, session_factory)

    response = await client.post(
        "/api/admin/users",
        json={"email": "второй@example.com", "name": "Второй", "role": RoleCode.ADMIN},
    )

    assert response.status_code == 403


async def test_superadmin_can_create_an_admin(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in_admin(client, session_factory, role=RoleCode.SUPERADMIN, email=SUPER)

    response = await client.post(
        "/api/admin/users",
        json={"email": "новый@example.com", "name": "Новый", "role": RoleCode.ADMIN},
    )

    assert response.status_code == 201
    assert response.json()["user"]["role"] == RoleCode.ADMIN


async def test_account_is_deleted(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in_admin(client, session_factory)
    victim = await add_user(session_factory)

    response = await client.delete(f"/api/admin/users/{victim.id}")

    assert response.status_code == 204
    async with session_factory() as session:
        assert await session.get(User, victim.id) is None


async def test_admin_cannot_delete_another_admin(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Админы не воюют друг с другом: разбирать такое некому, кроме суперадмина."""
    await sign_in_admin(client, session_factory)
    colleague = await add_user(session_factory, email="второй@example.com", role=RoleCode.ADMIN)

    assert (await client.delete(f"/api/admin/users/{colleague.id}")).status_code == 403


async def test_nobody_deletes_themselves(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Проще всего остаться без единого администратора именно так."""
    admin = await sign_in_admin(client, session_factory, role=RoleCode.SUPERADMIN, email=SUPER)

    assert (await client.delete(f"/api/admin/users/{admin.id}")).status_code == 400


async def test_account_can_be_switched_off(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Выключить мягче, чем удалить: история загрузок остаётся на месте."""
    await sign_in_admin(client, session_factory)
    victim = await add_user(session_factory)

    response = await client.patch(f"/api/admin/users/{victim.id}", json={"is_active": False})

    assert response.status_code == 200
    assert response.json()["is_active"] is False


async def test_admin_cannot_change_roles(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in_admin(client, session_factory)
    victim = await add_user(session_factory)

    response = await client.patch(f"/api/admin/users/{victim.id}", json={"role": RoleCode.ADMIN})

    assert response.status_code == 403


async def test_superadmin_changes_roles(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in_admin(client, session_factory, role=RoleCode.SUPERADMIN, email=SUPER)
    victim = await add_user(session_factory)

    response = await client.patch(f"/api/admin/users/{victim.id}", json={"role": RoleCode.ADMIN})

    assert response.status_code == 200
    assert response.json()["role"] == RoleCode.ADMIN


async def test_deletion_is_written_to_the_journal(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin = await sign_in_admin(client, session_factory)
    victim = await add_user(session_factory)

    await client.delete(f"/api/admin/users/{victim.id}")

    async with session_factory() as session:
        entry = await session.scalar(
            sa.select(AuditEntry).where(AuditEntry.action == "user.delete")
        )
    assert entry.actor_id == admin.id
    assert entry.target_id == str(victim.id)


async def test_creation_is_written_to_the_journal(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in_admin(client, session_factory)

    await client.post("/api/admin/users", json={"email": "новый@example.com", "name": "Новый"})

    async with session_factory() as session:
        entry = await session.scalar(
            sa.select(AuditEntry).where(AuditEntry.action == "user.create")
        )
    assert entry is not None
    # Пароль в журнал не попадает: журнал читают чаще, чем заводят учётки.
    assert "password" not in str(entry.payload)

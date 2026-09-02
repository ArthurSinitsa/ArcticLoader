"""Глобальные рубильники — раздел 3.6 плана.

Волна ботов или подозрительная активность должна лечиться галочкой, а не
срочным деплоем ночью. Поэтому флаги живут в БД, а не в `.env`: перезапускать
контейнер ради них нельзя.
"""

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditEntry
from app.services import flags
from app.services.roles import RoleCode
from tests.test_auth import add_user, login

ADMIN = "admin@example.com"
SUPER = "super@example.com"
DOWNLOAD = {"url": "https://example.com/v.mp4", "quality": "720p"}


async def test_defaults_are_sane_when_nothing_was_set(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пустая таблица — рабочее состояние сервиса, а не повод падать."""
    async with session_factory() as session:
        state = await flags.current(session)

    assert state[flags.GUEST_ACCESS] is True
    assert state[flags.REGISTRATION] is True
    assert state[flags.MAINTENANCE] is False


async def test_flag_survives_reading_back(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await flags.set_flag(session, flags.MAINTENANCE, True)

        state = await flags.current(session)

    assert state[flags.MAINTENANCE] is True


async def test_admin_cannot_flip_switches(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Рубильники — за суперадмином: это решения уровня всего сервиса."""
    await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN)
    await login(client, email=ADMIN)

    response = await client.put(f"/api/admin/flags/{flags.GUEST_ACCESS}", json={"enabled": False})

    assert response.status_code == 403


async def test_superadmin_flips_the_switch(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory, email=SUPER, role=RoleCode.SUPERADMIN)
    await login(client, email=SUPER)

    response = await client.put(f"/api/admin/flags/{flags.GUEST_ACCESS}", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()[flags.GUEST_ACCESS] is False


async def test_unknown_flag_is_refused(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Список флагов закрыт: иначе в таблицу натечёт мусор из опечаток."""
    await add_user(session_factory, email=SUPER, role=RoleCode.SUPERADMIN)
    await login(client, email=SUPER)

    assert (
        await client.put("/api/admin/flags/выдуманный", json={"enabled": True})
    ).status_code == 404


async def test_switch_is_written_to_the_journal(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory, email=SUPER, role=RoleCode.SUPERADMIN)
    await login(client, email=SUPER)

    await client.put(f"/api/admin/flags/{flags.MAINTENANCE}", json={"enabled": True})

    async with session_factory() as session:
        entry = await session.scalar(sa.select(AuditEntry).where(AuditEntry.action == "flag.set"))
    assert entry.target_id == flags.MAINTENANCE


async def test_guest_switch_closes_anonymous_downloads(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        await flags.set_flag(session, flags.GUEST_ACCESS, False)

    assert (await client.post("/api/downloads", json=DOWNLOAD)).status_code == 403


async def test_maintenance_stops_new_downloads_for_everyone(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Очередь дорабатывает начатое, новые задачи не берутся — даже у админа."""
    await add_user(session_factory, email=SUPER, role=RoleCode.SUPERADMIN)
    await login(client, email=SUPER)
    async with session_factory() as session:
        await flags.set_flag(session, flags.MAINTENANCE, True)

    response = await client.post("/api/downloads", json=DOWNLOAD)

    assert response.status_code == 503


async def test_maintenance_does_not_close_the_admin_section(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Иначе рубильник нечем будет выключить обратно."""
    await add_user(session_factory, email=SUPER, role=RoleCode.SUPERADMIN)
    await login(client, email=SUPER)
    async with session_factory() as session:
        await flags.set_flag(session, flags.MAINTENANCE, True)

    assert (await client.get("/api/admin/flags")).status_code == 200

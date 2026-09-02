"""Активные задачи всех пользователей — матрица 2.5.1.

Админ смотрит, что происходит на сервере прямо сейчас: без этого прерывать
нечего, а искать зависшую загрузку приходится в логах.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import TaskStatus
from app.services.roles import RoleCode
from tests.test_auth import add_user, login
from tests.test_cancel import make_task

ADMIN = "admin@example.com"


async def test_plain_user_is_refused(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory)
    await login(client)

    assert (await client.get("/api/admin/tasks")).status_code == 403


async def test_admin_sees_tasks_of_everyone(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    owner = await add_user(session_factory, email="владелец@example.com")
    await make_task(session_factory, user_id=owner.id, status=TaskStatus.DOWNLOADING)
    await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN)
    await login(client, email=ADMIN)

    body = (await client.get("/api/admin/tasks")).json()

    assert body["total"] == 1
    assert body["items"][0]["owner"] == "владелец@example.com"


async def test_guest_task_is_visible_too(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Гостевые загрузки тоже занимают канал — админ должен их видеть."""
    await make_task(session_factory, status=TaskStatus.DOWNLOADING)
    await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN)
    await login(client, email=ADMIN)

    body = (await client.get("/api/admin/tasks")).json()

    assert body["total"] == 1
    assert body["items"][0]["owner"] is None


async def test_finished_tasks_are_hidden_by_default(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Экран про «сейчас», а не про историю: завершённым тут не место."""
    await make_task(session_factory, status=TaskStatus.READY)
    await make_task(session_factory, status=TaskStatus.QUEUED)
    await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN)
    await login(client, email=ADMIN)

    body = (await client.get("/api/admin/tasks")).json()

    assert body["total"] == 1
    assert body["items"][0]["status"] == TaskStatus.QUEUED


async def test_history_can_be_requested_explicitly(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await make_task(session_factory, status=TaskStatus.READY)
    await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN)
    await login(client, email=ADMIN)

    body = (await client.get("/api/admin/tasks?active_only=false")).json()

    assert body["total"] == 1

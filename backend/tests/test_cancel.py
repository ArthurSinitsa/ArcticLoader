"""Прерывание задач — раздел 2.5.3 плана.

Снять job с очереди недостаточно: yt-dlp и ffmpeg живут отдельными процессами.
Здесь проверяется видимая часть процедуры — статус, возврат квоты, права и
команда воркеру; сама остановка процесса живёт в воркере.
"""

import uuid

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditEntry, Task, TaskStatus
from app.services import events
from app.services import tasks as tasks_repo
from app.services.formats import Quality
from app.services.roles import RoleCode
from tests.conftest import FakeArqPool
from tests.test_auth import add_user, login

ADMIN = "admin@example.com"


async def make_task(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID | None = None,
    status: TaskStatus = TaskStatus.QUEUED,
) -> Task:
    async with session_factory() as session:
        task = await tasks_repo.create_task(
            session, source_url="https://example.com/v", quality=Quality.P720, user_id=user_id
        )
        if status is not TaskStatus.QUEUED:
            await session.execute(sa.update(Task).where(Task.id == task.id).values(status=status))
            await session.commit()
        return task


async def status_of(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> TaskStatus:
    async with session_factory() as session:
        return (await session.get(Task, task_id)).status


async def test_owner_cancels_own_task(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user = await add_user(session_factory)
    task = await make_task(session_factory, user_id=user.id)
    await login(client)

    response = await client.post(f"/api/downloads/{task.id}/cancel")

    assert response.status_code == 204
    assert await status_of(session_factory, task.id) is TaskStatus.CANCELLED


async def test_stranger_cannot_cancel(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    owner = await add_user(session_factory, email="владелец@example.com")
    task = await make_task(session_factory, user_id=owner.id)
    await add_user(session_factory)
    await login(client)

    assert (await client.post(f"/api/downloads/{task.id}/cancel")).status_code == 403


async def test_admin_cancels_any_task(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    owner = await add_user(session_factory, email="владелец@example.com")
    task = await make_task(session_factory, user_id=owner.id)
    await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN)
    await login(client, email=ADMIN)

    assert (await client.post(f"/api/downloads/{task.id}/cancel")).status_code == 204


async def test_guest_cannot_cancel(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """По матрице 2.5.1 отмена — возможность учётной записи, не гостя."""
    task = await make_task(session_factory)

    assert (await client.post(f"/api/downloads/{task.id}/cancel")).status_code == 401


async def test_finished_task_cannot_be_cancelled(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Файл уже скачан: «отмена» задним числом только запутает историю."""
    user = await add_user(session_factory)
    task = await make_task(session_factory, user_id=user.id, status=TaskStatus.READY)
    await login(client)

    response = await client.post(f"/api/downloads/{task.id}/cancel")

    assert response.status_code == 409
    assert await status_of(session_factory, task.id) is TaskStatus.READY


async def test_missing_task_answers_404(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory)
    await login(client)

    assert (await client.post(f"/api/downloads/{uuid.uuid4()}/cancel")).status_code == 404


async def test_worker_is_told_to_stop(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    arq_pool: FakeArqPool,
) -> None:
    """Пометки в БД мало: процесс yt-dlp живёт в воркере и сам не остановится."""
    user = await add_user(session_factory)
    task = await make_task(session_factory, user_id=user.id, status=TaskStatus.DOWNLOADING)
    await login(client)

    await client.post(f"/api/downloads/{task.id}/cancel")

    assert (events.CANCEL_CHANNEL, {"task_id": str(task.id)}) in arq_pool.published


async def test_cancelled_before_download_returns_the_quota(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Задача не начала расходовать трафик — квоту за неё не берём (2.5.3)."""
    await add_user(session_factory)
    await login(client)
    first = await client.post(
        "/api/downloads", json={"url": "https://example.com/a.mp4", "quality": "720p"}
    )
    task_id = first.json()["task_id"]

    await client.post(f"/api/downloads/{task_id}/cancel")

    # Пользователю положено четыре параллельных: проверяем не слот, а квоту —
    # четыре новые загрузки подряд пройдут только если токен вернулся.
    for index in range(4):
        response = await client.post(
            "/api/downloads",
            json={"url": f"https://example.com/{index}.mp4", "quality": "720p"},
        )
        assert response.status_code == 202


async def test_cancellation_is_written_to_the_journal(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Прерывание чужой задачи — ровно тот случай, ради которого журнал заведён."""
    owner = await add_user(session_factory, email="владелец@example.com")
    task = await make_task(session_factory, user_id=owner.id)
    admin = await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN)
    await login(client, email=ADMIN)

    await client.post(f"/api/downloads/{task.id}/cancel")

    async with session_factory() as session:
        entry = await session.scalar(
            sa.select(AuditEntry).where(AuditEntry.action == "task.cancel")
        )
    assert entry.actor_id == admin.id
    assert entry.target_id == str(task.id)


async def test_own_cancellation_is_not_journalled(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Журнал — про власть над чужими данными, а не про обычную работу."""
    user = await add_user(session_factory)
    task = await make_task(session_factory, user_id=user.id)
    await login(client)

    await client.post(f"/api/downloads/{task.id}/cancel")

    async with session_factory() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(AuditEntry))
    assert count == 0

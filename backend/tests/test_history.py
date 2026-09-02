"""История загрузок — матрица 2.5.1.

Гостю история не положена: у него нет учётки, а связывать её с отпечатком
означало бы показывать чужие загрузки всем, кто сидит за тем же NAT с тем же
браузером.
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Task, TaskStatus
from app.services import tasks as tasks_repo
from app.services.formats import Quality
from tests.test_auth import add_user, login


async def add_task(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    user_id=None,
    url: str = "https://example.com/v.mp4",
    status: TaskStatus = TaskStatus.READY,
    file_path: str | None = None,
    created_at: datetime | None = None,
) -> Task:
    async with session_factory() as session:
        task = await tasks_repo.create_task(
            session, source_url=url, quality=Quality.P720, user_id=user_id
        )
        await session.execute(
            sa.update(Task)
            .where(Task.id == task.id)
            .values(
                status=status,
                file_path=file_path or str(settings.media_root / str(task.id) / "video.mp4"),
                **({"created_at": created_at} if created_at else {}),
            )
        )
        await session.commit()
        return task


async def test_guest_has_no_history(client: AsyncClient) -> None:
    assert (await client.get("/api/downloads")).status_code == 401


async def test_history_lists_own_downloads(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    user = await add_user(session_factory)
    await add_task(session_factory, settings, user_id=user.id)
    await login(client)

    body = (await client.get("/api/downloads")).json()

    assert body["total"] == 1
    assert body["items"][0]["source_url"] == "https://example.com/v.mp4"


async def test_history_hides_other_peoples_downloads(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    user = await add_user(session_factory)
    stranger = await add_user(session_factory, email="stranger@example.com")
    await add_task(
        session_factory, settings, user_id=stranger.id, url="https://example.com/чужое.mp4"
    )
    await add_task(session_factory, settings, user_id=user.id)
    await login(client)

    body = (await client.get("/api/downloads")).json()

    assert body["total"] == 1
    assert "чужое" not in body["items"][0]["source_url"]


async def test_guest_tasks_are_not_in_anyones_history(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    await add_user(session_factory)
    await add_task(session_factory, settings, user_id=None)
    await login(client)

    assert (await client.get("/api/downloads")).json()["total"] == 0


async def test_newest_download_comes_first(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    user = await add_user(session_factory)
    # Время задаётся явно: `now()` в SQLite имеет разрешение в секунду, обе
    # записи получили бы одинаковую метку, и порядок решал бы случайный UUID.
    await add_task(
        session_factory,
        settings,
        user_id=user.id,
        url="https://example.com/старое.mp4",
        created_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    )
    await add_task(
        session_factory,
        settings,
        user_id=user.id,
        url="https://example.com/новое.mp4",
        created_at=datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
    )
    await login(client)

    body = (await client.get("/api/downloads")).json()

    assert "новое" in body["items"][0]["source_url"]


async def test_history_is_paginated(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    user = await add_user(session_factory)
    for index in range(3):
        await add_task(
            session_factory, settings, user_id=user.id, url=f"https://example.com/{index}.mp4"
        )
    await login(client)

    body = (await client.get("/api/downloads?limit=2")).json()

    assert len(body["items"]) == 2
    # Общее число — не длина страницы, иначе UI не покажет, что дальше есть ещё.
    assert body["total"] == 3


async def test_second_page_continues_the_list(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    user = await add_user(session_factory)
    for index in range(3):
        await add_task(
            session_factory, settings, user_id=user.id, url=f"https://example.com/{index}.mp4"
        )
    await login(client)

    first = (await client.get("/api/downloads?limit=2")).json()["items"]
    second = (await client.get("/api/downloads?limit=2&offset=2")).json()["items"]

    assert len(second) == 1
    assert {item["id"] for item in first}.isdisjoint({item["id"] for item in second})


async def test_ready_download_keeps_its_link(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """История без ссылки бесполезна: файл ещё жив, а забрать его нечем."""
    user = await add_user(session_factory)
    await add_task(session_factory, settings, user_id=user.id)
    await login(client)

    body = (await client.get("/api/downloads")).json()

    assert body["items"][0]["download_url"].startswith("http://testserver/files/")


async def test_page_size_has_a_ceiling(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Иначе `?limit=100000` выгружает всю таблицу одним запросом."""
    await add_user(session_factory)
    await login(client)

    assert (await client.get("/api/downloads?limit=5000")).status_code == 422


async def test_file_outside_the_media_root_does_not_break_history(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Каталог загрузок могли переназначить — старые записи не должны
    ронять всю историю пятисоткой."""
    user = await add_user(session_factory)
    await add_task(session_factory, settings, user_id=user.id, file_path="/иной/том/video.mp4")
    await login(client)

    response = await client.get("/api/downloads")

    assert response.status_code == 200
    assert response.json()["items"][0]["download_url"] is None

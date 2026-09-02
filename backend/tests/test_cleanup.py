"""Чистка истёкших загрузок — раздел 3 плана.

Диск ноутбука не резиновый: файлы живут двое суток, дальше их место дороже
их содержимого. То же и с кэшем метаданных — он и задуман коротким.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Task, TaskStatus, UrlMeta
from app.services import storage
from app.services import tasks as tasks_repo
from app.services.formats import Quality
from app.worker.cleanup import cleanup_task

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def ctx(settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    return {"settings": settings, "session_factory": session_factory}


async def make_ready_task(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    expires_at: datetime | None,
    with_file: bool = True,
    status: TaskStatus = TaskStatus.READY,
) -> tuple[uuid.UUID, Path]:
    async with session_factory() as session:
        task = await tasks_repo.create_task(
            session, source_url="https://example.com/v", quality=Quality.P720
        )
        task_id = task.id

    directory = storage.task_dir(settings.media_root, task_id)
    file_path = directory / "video.mp4"
    if with_file:
        directory.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"payload")

    async with session_factory() as session:
        await session.execute(
            sa.update(Task)
            .where(Task.id == task_id)
            .values(
                status=status,
                expires_at=expires_at,
                file_path=str(file_path) if with_file else None,
            )
        )
        await session.commit()

    return task_id, file_path


async def status_of(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> TaskStatus:
    async with session_factory() as session:
        return (await session.get(Task, task_id)).status


async def test_expired_file_is_deleted(
    ctx: dict[str, Any], settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    task_id, file_path = await make_ready_task(
        session_factory, settings, expires_at=NOW - timedelta(hours=1)
    )

    await cleanup_task(ctx, now=NOW)

    assert not file_path.exists()
    assert await status_of(session_factory, task_id) is TaskStatus.EXPIRED


async def test_expired_task_forgets_the_path(
    ctx: dict[str, Any], settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Ссылка на удалённый файл — обещание, которого сервис уже не сдержит."""
    task_id, _ = await make_ready_task(
        session_factory, settings, expires_at=NOW - timedelta(hours=1)
    )

    await cleanup_task(ctx, now=NOW)

    async with session_factory() as session:
        task = await session.get(Task, task_id)
    assert task.file_path is None


async def test_living_file_is_left_alone(
    ctx: dict[str, Any], settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    task_id, file_path = await make_ready_task(
        session_factory, settings, expires_at=NOW + timedelta(hours=1)
    )

    await cleanup_task(ctx, now=NOW)

    assert file_path.exists()
    assert await status_of(session_factory, task_id) is TaskStatus.READY


async def test_task_without_deadline_is_left_alone(
    ctx: dict[str, Any], settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Задачи, дожившие с прошлых этапов, без срока — не повод их сносить."""
    task_id, file_path = await make_ready_task(session_factory, settings, expires_at=None)

    await cleanup_task(ctx, now=NOW)

    assert file_path.exists()
    assert await status_of(session_factory, task_id) is TaskStatus.READY


async def test_failed_task_is_not_touched(
    ctx: dict[str, Any], settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Терминальный статус не переписываем: `failed` объясняет причину, а
    `expired` — нет."""
    task_id, _ = await make_ready_task(
        session_factory,
        settings,
        expires_at=NOW - timedelta(hours=1),
        status=TaskStatus.FAILED,
    )

    await cleanup_task(ctx, now=NOW)

    assert await status_of(session_factory, task_id) is TaskStatus.FAILED


async def test_direct_link_expires_without_files(
    ctx: dict[str, Any], settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """У прямой ссылки файла на сервере нет, но ссылка на CDN тоже протухает."""
    task_id, _ = await make_ready_task(
        session_factory, settings, expires_at=NOW - timedelta(hours=1), with_file=False
    )

    await cleanup_task(ctx, now=NOW)

    assert await status_of(session_factory, task_id) is TaskStatus.EXPIRED


async def test_stale_url_meta_is_purged(
    ctx: dict[str, Any], settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        session.add(
            UrlMeta(
                url_hash="старый",
                source_url="https://example.com/old",
                extractor="generic",
                payload={},
                fetched_at=NOW - timedelta(seconds=settings.url_meta_ttl_seconds + 60),
            )
        )
        session.add(
            UrlMeta(
                url_hash="свежий",
                source_url="https://example.com/new",
                extractor="generic",
                payload={},
                fetched_at=NOW,
            )
        )
        await session.commit()

    await cleanup_task(ctx, now=NOW)

    async with session_factory() as session:
        left = (await session.scalars(sa.select(UrlMeta.url_hash))).all()
    assert list(left) == ["свежий"]

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import TaskStatus
from app.services import tasks as tasks_repo
from app.services.formats import Quality


async def test_create_task_starts_queued(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        task = await tasks_repo.create_task(
            session, source_url="https://example.com/v", quality=Quality.P1080
        )

    assert task.status is TaskStatus.QUEUED
    assert task.progress == 0.0
    assert task.id is not None


async def test_update_task_writes_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        task = await tasks_repo.create_task(
            session, source_url="https://example.com/v", quality=Quality.P1080
        )
        await tasks_repo.update_task(session, task.id, progress=42.5, worker_pid=777)
        stored = await tasks_repo.get_task(session, task.id)

    assert stored.progress == 42.5
    assert stored.worker_pid == 777


async def test_set_status_does_not_revive_terminal_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отменённая задача не должна воскреснуть от догоняющего апдейта воркера."""
    async with session_factory() as session:
        task = await tasks_repo.create_task(
            session, source_url="https://example.com/v", quality=Quality.P1080
        )
        await tasks_repo.set_status(session, task.id, TaskStatus.CANCELLED)

        applied = await tasks_repo.set_status(session, task.id, TaskStatus.DOWNLOADING)
        stored = await tasks_repo.get_task(session, task.id)

    assert applied is False
    assert stored.status is TaskStatus.CANCELLED


async def test_set_status_reports_success_for_live_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        task = await tasks_repo.create_task(
            session, source_url="https://example.com/v", quality=Quality.P1080
        )

        applied = await tasks_repo.set_status(
            session, task.id, TaskStatus.EXTRACTING, extractor="Generic"
        )
        stored = await tasks_repo.get_task(session, task.id)

    assert applied is True
    assert stored.status is TaskStatus.EXTRACTING
    assert stored.extractor == "Generic"

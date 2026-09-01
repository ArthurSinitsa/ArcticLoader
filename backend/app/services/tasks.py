"""Операции над таблицей `tasks`."""

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TERMINAL_STATUSES, Task, TaskStatus

log = logging.getLogger(__name__)


async def create_task(session: AsyncSession, *, source_url: str, format_id: str | None) -> Task:
    task = Task(source_url=source_url, format_id=format_id, status=TaskStatus.QUEUED)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    log.info("задача создана: %s", source_url)
    return task


async def get_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    return await session.get(Task, task_id)


async def update_task(session: AsyncSession, task_id: uuid.UUID, **values: Any) -> None:
    """Точечное обновление полей. Воркер зовёт это часто — объект не грузим."""
    await session.execute(sa.update(Task).where(Task.id == task_id).values(**values))
    await session.commit()


async def set_status(
    session: AsyncSession, task_id: uuid.UUID, status: TaskStatus, **values: Any
) -> bool:
    """Смена статуса с защитой терминальных состояний.

    Возвращает False, если задача уже завершена или отменена — воркеру это
    сигнал прекратить работу, а не перезаписать чужой результат.
    """
    result = await session.execute(
        sa.update(Task)
        .where(Task.id == task_id, Task.status.not_in(TERMINAL_STATUSES))
        .values(status=status, **values)
    )
    await session.commit()

    changed = result.rowcount > 0
    if changed:
        log.info("статус -> %s", status.value)
    else:
        log.warning("статус %s не применён: задача уже в терминальном состоянии", status.value)
    return changed

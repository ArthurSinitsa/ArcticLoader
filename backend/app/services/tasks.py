"""Операции над таблицей `tasks`."""

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TERMINAL_STATUSES, Task, TaskStatus
from app.services.formats import Quality

log = logging.getLogger(__name__)


async def create_task(
    session: AsyncSession,
    *,
    source_url: str,
    quality: Quality,
    user_id: uuid.UUID | None = None,
    guest_fingerprint: str | None = None,
) -> Task:
    task = Task(
        source_url=source_url,
        quality=quality.value,
        status=TaskStatus.QUEUED,
        user_id=user_id,
        guest_fingerprint=guest_fingerprint,
    )
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


async def count_active(
    session: AsyncSession, *, user_id: uuid.UUID | None, guest_fingerprint: str | None
) -> int:
    """Сколько задач субъекта ещё в работе — параллельный лимит из 2.5.2.

    Считаем по БД, а не по счётчику в Redis: счётчик пришлось бы уменьшать в
    каждой ветке завершения, включая падение воркера, и он бы неминуемо
    разъехался с действительностью.
    """
    owner = (
        Task.user_id == user_id
        if user_id is not None
        else Task.guest_fingerprint == guest_fingerprint
    )
    return await session.scalar(
        sa.select(sa.func.count())
        .select_from(Task)
        .where(owner, Task.status.not_in(TERMINAL_STATUSES))
    )

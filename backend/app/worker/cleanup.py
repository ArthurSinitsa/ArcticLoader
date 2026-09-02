"""Чистка по расписанию — раздел 3 плана.

Файлы живут двое суток: то, что не забрали за это время, почти наверняка уже
не заберут, а место на ноутбуке дороже. Заодно выметается протухший кэш
метаданных — он и задуман коротким (раздел 3.1).

Задача идёт в тот же ARQ-воркер отдельной cron-функцией: поднимать ради неё
системный cron и второй контейнер незачем.
"""

import logging
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Task, TaskStatus
from app.services import storage
from app.services import url_meta as url_meta_repo

log = logging.getLogger(__name__)


async def cleanup_task(ctx: dict[str, Any], *, now: datetime | None = None) -> None:
    """Убрать истёкшие загрузки и протухшие метаданные."""
    settings: Settings = ctx["settings"]
    moment = now or datetime.now(UTC)

    async with ctx["session_factory"]() as session:
        expired = await _expire_due(session, settings, moment)
        stale = await url_meta_repo.purge_stale(
            session, ttl_seconds=settings.url_meta_ttl_seconds, now=moment
        )

    if expired or stale:
        log.info("чистка: истекло загрузок %s, метаданных %s", expired, stale)


async def _expire_due(session: AsyncSession, settings: Settings, now: datetime) -> int:
    """Снести файлы задач, чей срок вышел, и перевести их в `expired`.

    Берём только `ready`: у `failed` статус объясняет причину, а `expired` —
    нет, и переписывать его значило бы терять эту причину.
    """
    due = (
        await session.scalars(
            sa.select(Task).where(
                Task.status == TaskStatus.READY,
                Task.expires_at.is_not(None),
                Task.expires_at <= now,
            )
        )
    ).all()

    for task in due:
        # У прямой ссылки файла на сервере нет — но ссылка на CDN тоже
        # протухает, поэтому статус меняется в обоих случаях.
        storage.remove_task_dir(settings.media_root, task.id)
        task.status = TaskStatus.EXPIRED
        # Ссылка на удалённый файл — обещание, которого сервис уже не сдержит.
        task.file_path = None
        task.direct_url = None

    if due:
        await session.commit()
    return len(due)

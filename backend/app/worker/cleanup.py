"""Чистка по расписанию — раздел 3 плана.

Файлы живут двое суток: то, что не забрали за это время, почти наверняка уже
не заберут, а место на ноутбуке дороже. Заодно выметается протухший кэш
метаданных — он и задуман коротким (раздел 3.1).

Задача идёт в тот же ARQ-воркер отдельной cron-функцией: поднимать ради неё
системный cron и второй контейнер незачем.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import RegistrationRequest, RequestStatus, Task, TaskStatus, User
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
        burnt = await _burn_unactivated(session, moment)
        missing = await _forget_missing_files(session, settings)

    if expired or stale or burnt or missing:
        log.info(
            "чистка: истекло загрузок %s, метаданных %s, учёток %s, потеряно файлов %s",
            expired,
            stale,
            burnt,
            missing,
        )


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


async def _burn_unactivated(session: AsyncSession, now: datetime) -> int:
    """Снести учётки, чей временный пароль так и не сменили, — раздел 2.6.

    «Не активировали — заявка сгорает, учётка не создаётся»: снаружи это
    выглядит именно так. Заявка при этом остаётся и помечается сгоревшей —
    по ней видно, что человек до сервиса не дошёл.
    """
    due = (
        await session.scalars(
            sa.select(User).where(
                User.must_change_password.is_(True),
                User.password_expires_at.is_not(None),
                User.password_expires_at <= now,
            )
        )
    ).all()

    for user in due:
        request = await session.scalar(
            sa.select(RegistrationRequest).where(
                RegistrationRequest.email == user.email,
                RegistrationRequest.status == RequestStatus.APPROVED,
            )
        )
        if request is not None:
            request.status = RequestStatus.EXPIRED
            request.processed_at = now
        await session.delete(user)
        log.info("учётка %s не активирована в срок — удалена", user.email)

    if due:
        await session.commit()
    return len(due)


async def _forget_missing_files(session: AsyncSession, settings: Settings) -> int:
    """Снять «готово» с задач, чьих файлов больше нет на диске.

    Файл могли удалить мимо сервиса — руками, при переезде каталога или после
    сбоя диска. Задача при этом остаётся `ready` со ссылкой в никуда, и
    история обещает то, чего уже нет.

    Если каталога загрузок нет вовсе, не делаем ничего: это почти наверняка
    непримонтированный том, и «файла нет» окажется верно сразу для всех — так
    вся история потерялась бы из-за ошибки инфраструктуры, а не по делу.
    """
    if not settings.media_root.exists():
        log.warning("каталог загрузок недоступен — проверку файлов пропускаю")
        return 0

    candidates = (
        await session.scalars(
            sa.select(Task).where(
                Task.status == TaskStatus.READY,
                Task.file_path.is_not(None),
                # У прямой ссылки файла на сервере и не должно быть.
                Task.direct_url.is_(None),
            )
        )
    ).all()

    lost = [task for task in candidates if not Path(task.file_path).exists()]
    for task in lost:
        task.status = TaskStatus.EXPIRED
        task.file_path = None
        log.info("файл задачи %s пропал с диска — задача больше не «готова»", task.id)

    if lost:
        await session.commit()
    return len(lost)

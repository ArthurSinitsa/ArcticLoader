"""Задача скачивания — единственная функция воркера на Этапе 1."""

import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logging_setup import task_context
from app.models import Task, TaskStatus
from app.services import storage, ytdlp
from app.services import tasks as tasks_repo

log = logging.getLogger(__name__)

#: Извлечение метаданных не должно занимать столько же, сколько сама загрузка.
PROBE_TIMEOUT_SECONDS = 120.0
PROGRESS_INTERVAL_SECONDS = 1.0


class _TaskFinishedError(Exception):
    """Задача перестала быть нашей: её уже завершили или отменили."""


async def download_task(ctx: dict[str, Any], task_id: str) -> None:
    with task_context(task_id):
        async with ctx["session_factory"]() as session:
            await _process(session, ctx["settings"], uuid.UUID(task_id))


async def _process(session: AsyncSession, settings: Settings, task_id: uuid.UUID) -> None:
    task = await tasks_repo.get_task(session, task_id)
    if task is None:
        log.error("задача не найдена в БД")
        return
    if task.status is not TaskStatus.QUEUED:
        log.warning("задача уже в статусе %s, пропускаю", task.status.value)
        return

    log.info("взял в работу: %s", task.source_url)
    try:
        info = await _extract(session, task)
        file_path = await _download(session, settings, task, info)
    except _TaskFinishedError:
        log.info("обработка прервана: задача завершена извне")
        return
    except ytdlp.YtDlpError as exc:
        await _fail(session, task_id, exc.code, str(exc))
        return
    except Exception as exc:
        log.exception("непредвиденная ошибка при обработке задачи")
        await _fail(session, task_id, ytdlp.ERROR_UNKNOWN, str(exc))
        return

    size = file_path.stat().st_size
    await tasks_repo.set_status(
        session,
        task_id,
        TaskStatus.READY,
        progress=100.0,
        file_path=str(file_path),
        file_size=size,
        worker_pid=None,
        finished_at=_now(),
    )
    log.info("готово: %s, %.1f МБ", file_path.name, size / 1024 / 1024)


async def _extract(session: AsyncSession, task: Task) -> ytdlp.MediaInfo:
    if not await tasks_repo.set_status(session, task.id, TaskStatus.EXTRACTING):
        raise _TaskFinishedError

    info = await ytdlp.probe(task.source_url, timeout=PROBE_TIMEOUT_SECONDS)
    log.info("метаданные: %r, экстрактор %s", info.title, info.extractor)
    await tasks_repo.update_task(session, task.id, title=info.title, extractor=info.extractor)
    return info


async def _download(
    session: AsyncSession, settings: Settings, task: Task, info: ytdlp.MediaInfo
) -> Path:
    if not await tasks_repo.set_status(session, task.id, TaskStatus.DOWNLOADING):
        raise _TaskFinishedError

    async def remember_pid(pid: int) -> None:
        # PID нужен, чтобы админ мог прервать задачу — раздел 2.5.3 плана.
        await tasks_repo.update_task(session, task.id, worker_pid=pid)

    return await ytdlp.download(
        task.source_url,
        format_spec=task.format_id or settings.default_format,
        dest_dir=storage.task_dir(settings.media_root, task.id),
        timeout=settings.download_timeout_seconds,
        on_start=remember_pid,
        on_progress=_ProgressReporter(session, task.id),
    )


async def _fail(session: AsyncSession, task_id: uuid.UUID, code: str, message: str) -> None:
    log.error("задача провалена (%s): %s", code, message)
    await tasks_repo.set_status(
        session,
        task_id,
        TaskStatus.FAILED,
        error_code=code,
        error_message=message,
        worker_pid=None,
        finished_at=_now(),
    )


class _ProgressReporter:
    """Пишет прогресс в БД не чаще раза в секунду.

    yt-dlp шлёт обновления десятками в секунду; без троттлинга раздела 2.3
    плана каждая загрузка превращается в поток UPDATE-запросов.
    """

    def __init__(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        interval: float = PROGRESS_INTERVAL_SECONDS,
    ) -> None:
        self._session = session
        self._task_id = task_id
        self._interval = interval
        self._last_write = 0.0

    async def __call__(self, progress: ytdlp.Progress) -> None:
        percent = progress.percent
        if percent is None:
            return

        now = time.monotonic()
        if now - self._last_write < self._interval:
            return
        self._last_write = now

        await tasks_repo.update_task(self._session, self._task_id, progress=round(percent, 1))
        log.debug("прогресс %.1f%%", percent)


def _now() -> datetime:
    return datetime.now(UTC)

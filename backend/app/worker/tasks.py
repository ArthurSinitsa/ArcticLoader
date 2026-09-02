"""Задача скачивания."""

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arq.worker import Retry
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logging_setup import task_context
from app.models import Task, TaskStatus
from app.services import events, formats, storage, ytdlp
from app.services import tasks as tasks_repo
from app.services.formats import Quality

log = logging.getLogger(__name__)

#: Извлечение метаданных не должно занимать столько же, сколько сама загрузка.
PROBE_TIMEOUT_SECONDS = 120.0
PROGRESS_INTERVAL_SECONDS = 1.0

#: Сто процентов принадлежат статусу `ready`. Пока идёт скачивание, полоска
#: не должна упираться в потолок: после неё ещё ремукс.
MAX_DOWNLOAD_PERCENT = 99.0

MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 15

#: Пауза, пока чистка освобождает место. Пять минут: чаще дёргать диск
#: бессмысленно, реже — задача простаивает.
DISK_RETRY_DELAY_SECONDS = 300


class _TaskFinishedError(Exception):
    """Задача перестала быть нашей: её уже завершили или отменили."""


async def download_task(ctx: dict[str, Any], task_id: str) -> None:
    with task_context(task_id):
        async with ctx["session_factory"]() as session:
            job = _Job(
                session=session,
                redis=ctx["redis"],
                settings=ctx["settings"],
                task_id=uuid.UUID(task_id),
                attempt=ctx.get("job_try", 1),
            )
            await job.run()


class _Job:
    """Одна загрузка от постановки до терминального статуса."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        redis: Redis,
        settings: Settings,
        task_id: uuid.UUID,
        attempt: int,
    ) -> None:
        self._session = session
        self._redis = redis
        self._settings = settings
        self._task_id = task_id
        self._attempt = attempt

    async def run(self) -> None:
        task = await tasks_repo.get_task(self._session, self._task_id)
        if task is None:
            log.error("задача не найдена в БД")
            return
        if task.status is not TaskStatus.QUEUED:
            log.warning("задача уже в статусе %s, пропускаю", task.status.value)
            return

        quality = Quality(task.quality)
        log.info(
            "взял в работу (попытка %s): %s, качество %s",
            self._attempt,
            task.source_url,
            quality.value,
        )

        self._require_free_space()

        try:
            info = await self._extract(task)

            # Развилка раздела 1.5: если видео и звук уже в одном контейнере и
            # качество от этого не падает, сервер в передаче не участвует.
            direct = self._pick_direct(info, quality)
            if direct is not None:
                await self._finish_direct(direct)
                return

            file_path = await self._download(task, quality, info)
        except _TaskFinishedError:
            log.info("обработка прервана: задача завершена извне")
            return
        except ytdlp.YtDlpError as exc:
            await self._handle_failure(exc.code, str(exc))
            return
        except Exception as exc:
            log.exception("непредвиденная ошибка при обработке задачи")
            await self._handle_failure(ytdlp.ERROR_UNKNOWN, str(exc))
            return

        await self._finish_file(file_path)

    def _deadline(self) -> datetime:
        return _now() + timedelta(hours=self._settings.file_ttl_hours)

    def _require_free_space(self) -> None:
        """Не начинать загрузку на переполненном диске — раздел 3.

        Начатая всё равно не доедет: место нужно и файлу, и ремуксу, которому
        на время работы требуется второй такой же. Задача остаётся в очереди
        и ждёт, пока чистка освободит место.
        """
        free = storage.free_space(self._settings.media_root)
        if free >= self._settings.min_free_disk_bytes:
            return

        log.error(
            "мало места на диске: свободно %.1f ГБ при пороге %.1f ГБ — очередь ждёт",
            free / 1024**3,
            self._settings.min_free_disk_bytes / 1024**3,
        )
        raise Retry(defer=DISK_RETRY_DELAY_SECONDS)

    async def _extract(self, task: Task) -> ytdlp.MediaInfo:
        await self._advance(TaskStatus.EXTRACTING)

        info = await ytdlp.probe(task.source_url, timeout=PROBE_TIMEOUT_SECONDS)
        log.info("метаданные: %r, экстрактор %s", info.title, info.extractor)
        await tasks_repo.update_task(
            self._session, self._task_id, title=info.title, extractor=info.extractor
        )
        return info

    def _pick_direct(self, info: ytdlp.MediaInfo, quality: Quality) -> ytdlp.MediaFormat | None:
        if not self._settings.direct_links_enabled:
            return None
        return formats.pick_progressive(info.formats, quality)

    async def _download(self, task: Task, quality: Quality, info: ytdlp.MediaInfo) -> Path:
        selector = formats.selector(quality)
        await self._advance(TaskStatus.DOWNLOADING, format_id=selector)

        reporter = _ProgressReporter(
            job=self,
            # Ожидаемый размер известен из метаданных: без него сквозной
            # процент по нескольким потокам не посчитать.
            expected_bytes=formats.estimate_size(info.formats, quality),
        )

        async def remember_pid(pid: int) -> None:
            # PID нужен, чтобы админ мог прервать задачу — раздел 2.5.3 плана.
            await tasks_repo.update_task(self._session, self._task_id, worker_pid=pid)

        return await ytdlp.download(
            task.source_url,
            format_spec=selector,
            format_sort=formats.FORMAT_SORT,
            dest_dir=storage.task_dir(self._settings.media_root, self._task_id),
            timeout=self._settings.download_timeout_seconds,
            on_start=remember_pid,
            on_progress=reporter.on_progress,
            on_postprocess=self._on_postprocess,
        )

    async def _on_postprocess(self, stage: ytdlp.PostProcess) -> None:
        if stage.is_merge_started:
            log.info("дорожки скачаны, идёт ремукс")
            await self._set_status(TaskStatus.PROCESSING, progress=MAX_DOWNLOAD_PERCENT)

    async def _finish_direct(self, chosen: ytdlp.MediaFormat) -> None:
        await self._set_status(
            TaskStatus.READY,
            progress=100.0,
            direct_url=chosen.url,
            file_size=chosen.filesize,
            format_id=chosen.format_id,
            worker_pid=None,
            finished_at=_now(),
            # Файла на сервере нет, но ссылка на CDN живёт часы — задача
            # тоже обязана однажды перестать притворяться готовой.
            expires_at=self._deadline(),
        )
        log.info(
            "готово без участия сервера: формат %s, %sp — клиент качает с CDN",
            chosen.format_id,
            chosen.height,
        )

    async def _finish_file(self, file_path: Path) -> None:
        size = file_path.stat().st_size
        await self._set_status(
            TaskStatus.READY,
            progress=100.0,
            file_path=str(file_path),
            file_size=size,
            worker_pid=None,
            finished_at=_now(),
            expires_at=self._deadline(),
        )
        log.info("готово: %s, %.1f МБ", file_path.name, size / 1024 / 1024)

    async def _handle_failure(self, code: str, message: str) -> None:
        if code in ytdlp.RETRYABLE_ERRORS and self._attempt < MAX_ATTEMPTS:
            delay = RETRY_BASE_DELAY_SECONDS * 2 ** (self._attempt - 1)
            log.warning(
                "ошибка %s, попытка %s из %s — повтор через %s с",
                code,
                self._attempt,
                MAX_ATTEMPTS,
                delay,
            )
            # Возвращаем в очередь, иначе следующая попытка увидит чужой статус
            # и откажется работать.
            if await self._set_status(TaskStatus.QUEUED, attempts=self._attempt, worker_pid=None):
                raise Retry(defer=delay)
            return

        log.error("задача провалена (%s): %s", code, message)
        await self._set_status(
            TaskStatus.FAILED,
            error_code=code,
            error_message=message,
            attempts=self._attempt,
            worker_pid=None,
            finished_at=_now(),
        )

    async def _set_status(self, status: TaskStatus, **values: Any) -> bool:
        """Сменить статус и разослать событие. False — задачу уже завершили."""
        if not await tasks_repo.set_status(self._session, self._task_id, status, **values):
            return False
        await self._announce(status, values.get("progress"))
        return True

    async def _advance(self, status: TaskStatus, **values: Any) -> None:
        """Шаг конвейера: если задачу увели, продолжать бессмысленно."""
        if not await self._set_status(status, **values):
            raise _TaskFinishedError

    async def _announce(self, status: TaskStatus, progress: float | None) -> None:
        task = await tasks_repo.get_task(self._session, self._task_id)
        if task is None:
            return
        await events.publish(
            self._redis,
            self._task_id,
            {
                "status": task.status.value,
                "progress": progress if progress is not None else task.progress,
                "title": task.title,
                "error_code": task.error_code,
            },
        )

    async def report_progress(self, percent: float) -> None:
        await tasks_repo.update_task(self._session, self._task_id, progress=percent)
        await events.publish(
            self._redis,
            self._task_id,
            {"status": TaskStatus.DOWNLOADING.value, "progress": percent},
        )


class _ProgressReporter:
    """Сквозной прогресс задачи, а не отдельного потока.

    DASH качается двумя проходами — сначала видео, потом звук, — и каждый
    рапортует свои 0–100%. Без накопления полоска дважды бежит до конца.
    Пишем не чаще раза в секунду: иначе yt-dlp завалит и БД, и Redis.
    """

    def __init__(
        self,
        *,
        job: _Job,
        expected_bytes: int | None,
        interval: float = PROGRESS_INTERVAL_SECONDS,
    ) -> None:
        self._job = job
        self._expected_bytes = expected_bytes
        self._interval = interval
        self._completed_bytes = 0
        self._last_percent = 0.0
        #: None, а не 0.0: нулём в шкале monotonic отмечена загрузка машины,
        #: и воркер, стартовавший в первую минуту, проглотил бы первый отчёт.
        self._last_write: float | None = None

    async def on_progress(self, progress: ytdlp.Progress) -> None:
        if progress.status == "finished":
            self._completed_bytes += progress.total_bytes or progress.downloaded_bytes or 0
            return

        percent = self._overall(progress)
        if percent is None:
            return

        now = time.monotonic()
        if self._last_write is not None and now - self._last_write < self._interval:
            return
        self._last_write = now

        await self._job.report_progress(percent)
        log.debug("прогресс %.1f%%", percent)

    def _overall(self, progress: ytdlp.Progress) -> float | None:
        done = self._completed_bytes + (progress.downloaded_bytes or 0)
        total = self._expected_bytes or (self._completed_bytes + (progress.total_bytes or 0))
        if not total:
            return None

        percent = min(done / total * 100, MAX_DOWNLOAD_PERCENT)
        # Оценка размера неточна, и при переходе к следующему потоку процент
        # может дёрнуться назад. Полоска, идущая вспять, выглядит поломкой.
        percent = max(percent, self._last_percent)
        self._last_percent = percent
        return round(percent, 1)


def _now() -> datetime:
    return datetime.now(UTC)

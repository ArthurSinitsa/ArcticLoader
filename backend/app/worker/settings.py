"""Точка входа воркера: `arq app.worker.settings.WorkerSettings`."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.cron import cron

from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.logging_setup import configure_logging
from app.worker.cleanup import cleanup_task
from app.worker.tasks import MAX_ATTEMPTS, download_task, watch_cancellations

log = logging.getLogger(__name__)

_settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(_settings.log_level)
    _settings.media_root.mkdir(parents=True, exist_ok=True)

    engine = create_engine(_settings.database_url)
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    ctx["settings"] = _settings
    #: Живые процессы yt-dlp: по этому реестру их находит команда прерывания.
    ctx["running"] = {}
    # Подписка на отмены живёт всё время работы воркера: команда приходит
    # извне, когда загрузка уже идёт (раздел 2.5.3).
    ctx["cancel_watcher"] = asyncio.create_task(watch_cancellations(ctx))

    log.info(
        "воркер запущен: одновременных загрузок %s, каталог %s, файлы живут %s ч",
        _settings.max_concurrent_downloads,
        _settings.media_root,
        _settings.file_ttl_hours,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    watcher = ctx.get("cancel_watcher")
    if watcher is not None:
        watcher.cancel()
    await ctx["engine"].dispose()
    log.info("воркер остановлен")


class WorkerSettings:
    functions: ClassVar[list[Callable[..., Any]]] = [download_task]

    #: Чистка живёт здесь же, отдельной cron-функцией: поднимать ради неё
    #: системный cron и второй контейнер незачем (раздел 3).
    cron_jobs: ClassVar[list[Any]] = [
        cron(
            cleanup_task,
            minute={
                minute for minute in range(60) if minute % _settings.cleanup_interval_minutes == 0
            },
            run_at_startup=True,
        )
    ]
    on_startup = startup
    on_shutdown = shutdown

    redis_settings = RedisSettings.from_dsn(_settings.redis_url)

    #: Глобальный семафор раздела 3 плана: стоит выше пользовательских квот и
    #: просто удерживает лишнее в очереди, вместо того чтобы тянуть всё сразу.
    max_jobs = _settings.max_concurrent_downloads

    job_timeout = _settings.download_timeout_seconds + 60

    #: Повторяет только то, что воркер сам просит через `Retry` — сетевые сбои
    #: и таймауты. Остальные ошибки сразу терминальны, см. RETRYABLE_ERRORS.
    max_tries = MAX_ATTEMPTS

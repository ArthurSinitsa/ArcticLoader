"""Точка входа воркера: `arq app.worker.settings.WorkerSettings`."""

import logging
from collections.abc import Callable
from typing import Any, ClassVar

from arq.connections import RedisSettings

from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.logging_setup import configure_logging
from app.worker.tasks import download_task

log = logging.getLogger(__name__)

_settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(_settings.log_level)
    _settings.media_root.mkdir(parents=True, exist_ok=True)

    engine = create_engine(_settings.database_url)
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    ctx["settings"] = _settings

    log.info(
        "воркер запущен: одновременных загрузок %s, каталог %s",
        _settings.max_concurrent_downloads,
        _settings.media_root,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["engine"].dispose()
    log.info("воркер остановлен")


class WorkerSettings:
    functions: ClassVar[list[Callable[..., Any]]] = [download_task]
    on_startup = startup
    on_shutdown = shutdown

    redis_settings = RedisSettings.from_dsn(_settings.redis_url)

    #: Глобальный семафор раздела 3 плана: стоит выше пользовательских квот и
    #: просто удерживает лишнее в очереди, вместо того чтобы тянуть всё сразу.
    max_jobs = _settings.max_concurrent_downloads

    job_timeout = _settings.download_timeout_seconds + 60

    #: Ретраи с backoff — Этап 2. Сейчас упавшая задача остаётся `failed`.
    max_tries = 1

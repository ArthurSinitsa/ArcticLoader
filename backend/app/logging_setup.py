"""Единая настройка логирования для API и воркера.

Идентификатор задачи живёт в contextvar и подставляется в каждую запись:
воркер тянет несколько загрузок параллельно, и без этого соотнести строку
лога с конкретной задачей невозможно.
"""

import logging
import logging.config
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

NO_TASK = "-"

_task_id: ContextVar[str] = ContextVar("task_id", default=NO_TASK)

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(task_id)s] %(name)s: %(message)s"


class TaskIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = _task_id.get()
        return True


@contextmanager
def task_context(task_id: object) -> Iterator[None]:
    """Помечает все записи внутри блока идентификатором задачи."""
    token = _task_id.set(str(task_id))
    try:
        yield
    finally:
        _task_id.reset(token)


def configure_logging(level: str = "INFO") -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"task_id": {"()": TaskIdFilter}},
            "formatters": {"default": {"format": LOG_FORMAT}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["task_id"],
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["console"], "level": level},
            # uvicorn и arq ставят свои обработчики — снимаем их, чтобы всё шло
            # через один формат, иначе в `docker compose logs` каша.
            "loggers": {
                name: {"handlers": [], "propagate": True}
                for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "arq")
            },
        }
    )

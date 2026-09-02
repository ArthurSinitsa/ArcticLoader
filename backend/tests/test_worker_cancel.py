"""Остановка процесса при отмене — шаги 2–4 процедуры 2.5.3.

Пометить задачу в БД мало: yt-dlp и ffmpeg — отдельные процессы, и без
сигнала они продолжат качать в никуда, занимая и канал, и место.
"""

import uuid
from typing import Any

import pytest

from app.config import Settings
from app.services import storage
from app.worker.tasks import cancel_running


class FakeProcess:
    """Процесс, который слушается SIGTERM."""

    def __init__(self, *, stubborn: bool = False) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._stubborn = stubborn

    def terminate(self) -> None:
        self.terminated = True
        if not self._stubborn:
            self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self._stubborn and self.returncode is None:
            # Не отвечает на SIGTERM: пусть решает таймаут.
            await __import__("asyncio").sleep(3600)
        return self.returncode or 0


@pytest.fixture
def ctx(settings: Settings) -> dict[str, Any]:
    return {"settings": settings, "running": {}}


async def test_running_process_is_stopped(ctx: dict[str, Any], settings: Settings) -> None:
    task_id = uuid.uuid4()
    process = FakeProcess()
    ctx["running"][task_id] = process

    stopped = await cancel_running(ctx, task_id)

    assert stopped
    assert process.terminated


async def test_partial_files_are_removed(ctx: dict[str, Any], settings: Settings) -> None:
    """Недокачанные `.part` не должны занимать диск после отмены."""
    task_id = uuid.uuid4()
    directory = storage.task_dir(settings.media_root, task_id)
    directory.mkdir(parents=True)
    (directory / "video.mp4.part").write_bytes(b"partial")
    ctx["running"][task_id] = FakeProcess()

    await cancel_running(ctx, task_id)

    assert not directory.exists()


async def test_process_leaves_the_registry(ctx: dict[str, Any]) -> None:
    """Иначе словарь растёт до перезапуска воркера."""
    task_id = uuid.uuid4()
    ctx["running"][task_id] = FakeProcess()

    await cancel_running(ctx, task_id)

    assert task_id not in ctx["running"]


async def test_unknown_task_is_ignored(ctx: dict[str, Any]) -> None:
    """Канал отмен общий: команда о чужой задаче — обычное дело, не ошибка."""
    assert not await cancel_running(ctx, uuid.uuid4())


async def test_stubborn_process_is_killed(ctx: dict[str, Any]) -> None:
    """Пять секунд на SIGTERM, дальше SIGKILL — иначе зависший ffmpeg вечен."""
    task_id = uuid.uuid4()
    process = FakeProcess(stubborn=True)
    ctx["running"][task_id] = process

    await cancel_running(ctx, task_id, grace=0.05)

    assert process.killed

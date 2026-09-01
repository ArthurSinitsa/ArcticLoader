import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import TaskStatus
from app.services import tasks as tasks_repo
from app.services import ytdlp
from app.worker import tasks as worker

SOURCE_URL = "https://example.com/watch?v=1"


@pytest.fixture
def ctx(settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    return {"settings": settings, "session_factory": session_factory}


async def make_task(session_factory: async_sessionmaker[AsyncSession], **values: Any) -> uuid.UUID:
    async with session_factory() as session:
        task = await tasks_repo.create_task(
            session, source_url=SOURCE_URL, format_id=values.pop("format_id", None)
        )
        if values:
            await tasks_repo.update_task(session, task.id, **values)
    return task.id


async def fetch_task(session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID) -> Any:
    async with session_factory() as session:
        return await tasks_repo.get_task(session, task_id)


def stub_probe(monkeypatch: pytest.MonkeyPatch, info: ytdlp.MediaInfo) -> None:
    async def probe(url: str, *, timeout: float) -> ytdlp.MediaInfo:
        return info

    monkeypatch.setattr(ytdlp, "probe", probe)


async def test_download_task_walks_to_ready(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await make_task(session_factory)
    stub_probe(monkeypatch, ytdlp.MediaInfo(title="Видео", extractor="Generic"))
    seen_format: list[str] = []

    async def download(url: str, **kwargs: Any) -> Path:
        seen_format.append(kwargs["format_spec"])
        await kwargs["on_start"](4242)
        await kwargs["on_progress"](
            ytdlp.Progress(status="downloading", downloaded_bytes=5, total_bytes=10)
        )
        destination: Path = kwargs["dest_dir"]
        destination.mkdir(parents=True, exist_ok=True)
        produced = destination / "video.mp4"
        produced.write_bytes(b"x" * 1024)
        return produced

    monkeypatch.setattr(ytdlp, "download", download)

    await worker.download_task(ctx, str(task_id))

    stored = await fetch_task(session_factory, task_id)
    assert stored.status is TaskStatus.READY
    assert stored.title == "Видео"
    assert stored.extractor == "Generic"
    assert stored.progress == 100.0
    assert stored.file_size == 1024
    assert stored.file_path.endswith("video.mp4")
    assert stored.finished_at is not None
    # PID снимается: процесса больше нет, прерывать нечего.
    assert stored.worker_pid is None
    assert seen_format == [settings.default_format]


async def test_download_task_uses_requested_format(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await make_task(session_factory, format_id="bv*[height<=480]+ba")
    stub_probe(monkeypatch, ytdlp.MediaInfo(title="Видео", extractor="Generic"))
    seen_format: list[str] = []

    async def download(url: str, **kwargs: Any) -> Path:
        seen_format.append(kwargs["format_spec"])
        destination: Path = kwargs["dest_dir"]
        destination.mkdir(parents=True, exist_ok=True)
        produced = destination / "video.mp4"
        produced.write_bytes(b"x")
        return produced

    monkeypatch.setattr(ytdlp, "download", download)

    await worker.download_task(ctx, str(task_id))

    assert seen_format == ["bv*[height<=480]+ba"]


async def test_download_task_records_error_code(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await make_task(session_factory)

    async def probe(url: str, *, timeout: float) -> ytdlp.MediaInfo:
        raise ytdlp.YtDlpError("unsupported_site", "Unsupported URL")

    monkeypatch.setattr(ytdlp, "probe", probe)

    await worker.download_task(ctx, str(task_id))

    stored = await fetch_task(session_factory, task_id)
    assert stored.status is TaskStatus.FAILED
    assert stored.error_code == "unsupported_site"
    assert stored.error_message == "Unsupported URL"
    assert stored.finished_at is not None


async def test_download_task_survives_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Одна битая задача не должна валить воркер."""
    task_id = await make_task(session_factory)

    async def probe(url: str, *, timeout: float) -> ytdlp.MediaInfo:
        raise RuntimeError("что-то сломалось")

    monkeypatch.setattr(ytdlp, "probe", probe)

    await worker.download_task(ctx, str(task_id))

    stored = await fetch_task(session_factory, task_id)
    assert stored.status is TaskStatus.FAILED
    assert stored.error_code == "unknown"


async def test_download_task_skips_task_that_is_not_queued(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await make_task(session_factory, status=TaskStatus.CANCELLED)

    async def probe(url: str, *, timeout: float) -> ytdlp.MediaInfo:
        raise AssertionError("отменённая задача не должна дойти до yt-dlp")

    monkeypatch.setattr(ytdlp, "probe", probe)

    await worker.download_task(ctx, str(task_id))

    stored = await fetch_task(session_factory, task_id)
    assert stored.status is TaskStatus.CANCELLED


async def test_download_task_ignores_missing_task(ctx: dict[str, Any]) -> None:
    await worker.download_task(ctx, str(uuid.uuid4()))


async def test_progress_reporter_throttles_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await make_task(session_factory)

    async with session_factory() as session:
        reporter = worker._ProgressReporter(session, task_id, interval=60.0)
        await reporter(ytdlp.Progress("downloading", downloaded_bytes=1, total_bytes=10))
        await reporter(ytdlp.Progress("downloading", downloaded_bytes=9, total_bytes=10))
        stored = await tasks_repo.get_task(session, task_id)

    # Второе обновление пришло раньше интервала — в БД осталось первое.
    assert stored.progress == 10.0


async def test_progress_reporter_skips_unknown_size(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await make_task(session_factory)

    async with session_factory() as session:
        reporter = worker._ProgressReporter(session, task_id)
        await reporter(ytdlp.Progress("downloading", downloaded_bytes=100))
        stored = await tasks_repo.get_task(session, task_id)

    assert stored.progress == 0.0

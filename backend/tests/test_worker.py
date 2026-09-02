import uuid
from pathlib import Path
from typing import Any

import pytest
from arq.worker import Retry
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Task, TaskStatus
from app.services import formats, ytdlp
from app.services import tasks as tasks_repo
from app.services.formats import Quality
from app.worker import tasks as worker
from tests.conftest import FakeArqPool

SOURCE_URL = "https://example.com/watch?v=1"

DASH_FORMATS = (
    ytdlp.MediaFormat(format_id="a", ext="m4a", vcodec="none", acodec="mp4a", filesize=1024),
    ytdlp.MediaFormat(format_id="v", ext="mp4", height=1080, vcodec="avc1", acodec="none"),
)

PROGRESSIVE_FORMATS = (
    ytdlp.MediaFormat(
        format_id="18",
        ext="mp4",
        height=360,
        vcodec="avc1",
        acodec="mp4a",
        filesize=8192,
        protocol="https",
        url="https://cdn.example.com/progressive.mp4",
    ),
)


@pytest.fixture
def ctx(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    arq_pool: FakeArqPool,
) -> dict[str, Any]:
    return {
        "settings": settings,
        "session_factory": session_factory,
        "redis": arq_pool,
        "job_try": 1,
    }


async def make_task(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    quality: Quality = Quality.P1080,
    **values: Any,
) -> uuid.UUID:
    async with session_factory() as session:
        task = await tasks_repo.create_task(session, source_url=SOURCE_URL, quality=quality)
        if values:
            await tasks_repo.update_task(session, task.id, **values)
    return task.id


async def fetch_task(session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID) -> Task:
    async with session_factory() as session:
        return await tasks_repo.get_task(session, task_id)


def stub_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    media_formats: tuple[ytdlp.MediaFormat, ...] = DASH_FORMATS,
) -> None:
    async def probe(url: str, *, timeout: float) -> ytdlp.MediaInfo:
        return ytdlp.MediaInfo(title="Видео", extractor="Generic", formats=media_formats)

    monkeypatch.setattr(ytdlp, "probe", probe)


def stub_download(monkeypatch: pytest.MonkeyPatch, *, size: int = 1024) -> list[dict[str, Any]]:
    """Подменяет скачивание и возвращает список полученных аргументов."""
    calls: list[dict[str, Any]] = []

    async def download(url: str, **kwargs: Any) -> Path:
        calls.append(kwargs)
        if kwargs.get("on_start"):
            await kwargs["on_start"](4242)
        if kwargs.get("on_progress"):
            await kwargs["on_progress"](
                ytdlp.Progress(status="downloading", downloaded_bytes=5, total_bytes=10)
            )
        destination: Path = kwargs["dest_dir"]
        destination.mkdir(parents=True, exist_ok=True)
        produced = destination / "video.mp4"
        produced.write_bytes(b"x" * size)
        return produced

    monkeypatch.setattr(ytdlp, "download", download)
    return calls


async def test_download_task_walks_to_ready(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await make_task(session_factory)
    stub_probe(monkeypatch)
    calls = stub_download(monkeypatch)

    await worker.download_task(ctx, str(task_id))

    stored = await fetch_task(session_factory, task_id)
    assert stored.status is TaskStatus.READY
    assert stored.title == "Видео"
    assert stored.progress == 100.0
    assert stored.file_size == 1024
    assert stored.direct_url is None
    # PID снимается: процесса больше нет, прерывать нечего.
    assert stored.worker_pid is None
    assert calls[0]["format_spec"] == formats.selector(Quality.P1080)
    assert calls[0]["format_sort"] == formats.FORMAT_SORT


async def test_download_task_expands_quality_into_selector(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await make_task(session_factory, quality=Quality.P480)
    stub_probe(monkeypatch)
    calls = stub_download(monkeypatch)

    await worker.download_task(ctx, str(task_id))

    stored = await fetch_task(session_factory, task_id)
    assert calls[0]["format_spec"] == "bv*[height<=480]+ba/b[height<=480]/b"
    assert stored.format_id == "bv*[height<=480]+ba/b[height<=480]/b"


async def test_progressive_format_skips_the_server(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Раздел 1.5: файл идёт с CDN, канал сервера не расходуется вовсе."""
    task_id = await make_task(session_factory, quality=Quality.P360)
    stub_probe(monkeypatch, media_formats=PROGRESSIVE_FORMATS)
    calls = stub_download(monkeypatch)

    await worker.download_task(ctx, str(task_id))

    stored = await fetch_task(session_factory, task_id)
    assert stored.status is TaskStatus.READY
    assert stored.direct_url == "https://cdn.example.com/progressive.mp4"
    assert stored.file_path is None
    assert stored.file_size == 8192
    assert calls == []


async def test_direct_links_can_be_switched_off(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Если площадка привяжет ссылки к IP, ветку надо гасить настройкой."""
    settings.direct_links_enabled = False
    task_id = await make_task(session_factory, quality=Quality.P360)
    stub_probe(monkeypatch, media_formats=PROGRESSIVE_FORMATS)
    calls = stub_download(monkeypatch)

    await worker.download_task(ctx, str(task_id))

    stored = await fetch_task(session_factory, task_id)
    assert stored.direct_url is None
    assert stored.file_path is not None
    assert len(calls) == 1


async def test_dash_format_is_downloaded_and_remuxed(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Раздельные дорожки прямой ссылкой не отдать — нужен ремукс на сервере."""
    task_id = await make_task(session_factory, quality=Quality.P1080)
    stub_probe(monkeypatch, media_formats=DASH_FORMATS)
    calls = stub_download(monkeypatch)

    await worker.download_task(ctx, str(task_id))

    stored = await fetch_task(session_factory, task_id)
    assert stored.direct_url is None
    assert len(calls) == 1


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


async def test_status_changes_are_published(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    arq_pool: FakeArqPool,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SSE питается этими событиями — без них полоска стоит до перезагрузки."""
    task_id = await make_task(session_factory)
    stub_probe(monkeypatch)
    stub_download(monkeypatch)

    await worker.download_task(ctx, str(task_id))

    channels = {channel for channel, _ in arq_pool.published}
    statuses = [payload["status"] for _, payload in arq_pool.published]

    assert channels == {f"task:{task_id}"}
    assert statuses[0] == "extracting"
    assert statuses[-1] == "ready"
    assert "downloading" in statuses


async def test_network_error_is_retried_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await make_task(session_factory)
    monkeypatch.setattr(ytdlp, "probe", _failing_probe(ytdlp.YtDlpError("network", "нет связи")))

    with pytest.raises(Retry) as raised:
        await worker.download_task(ctx, str(task_id))

    assert raised.value.defer_score == worker.RETRY_BASE_DELAY_SECONDS * 1000
    stored = await fetch_task(session_factory, task_id)
    # Возврат в очередь обязателен: иначе следующая попытка увидит чужой
    # статус и откажется работать.
    assert stored.status is TaskStatus.QUEUED
    assert stored.attempts == 1


async def test_retry_delay_grows_with_attempts(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx["job_try"] = 2
    task_id = await make_task(session_factory)
    monkeypatch.setattr(ytdlp, "probe", _failing_probe(ytdlp.YtDlpError("network", "нет связи")))

    with pytest.raises(Retry) as raised:
        await worker.download_task(ctx, str(task_id))

    assert raised.value.defer_score == worker.RETRY_BASE_DELAY_SECONDS * 2 * 1000


async def test_last_attempt_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx["job_try"] = worker.MAX_ATTEMPTS
    task_id = await make_task(session_factory)
    monkeypatch.setattr(ytdlp, "probe", _failing_probe(ytdlp.YtDlpError("network", "нет связи")))

    await worker.download_task(ctx, str(task_id))

    stored = await fetch_task(session_factory, task_id)
    assert stored.status is TaskStatus.FAILED
    assert stored.attempts == worker.MAX_ATTEMPTS


@pytest.mark.parametrize("code", ["geo_blocked", "login_required", "unsupported_site", "not_found"])
async def test_hopeless_errors_are_not_retried(
    code: str,
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Раздел 3.4: повторять то, что не чинится само, — только злить площадку."""
    task_id = await make_task(session_factory)
    monkeypatch.setattr(ytdlp, "probe", _failing_probe(ytdlp.YtDlpError(code, "нельзя")))

    await worker.download_task(ctx, str(task_id))

    stored = await fetch_task(session_factory, task_id)
    assert stored.status is TaskStatus.FAILED
    assert stored.error_code == code


async def test_merge_stage_switches_to_processing(
    monkeypatch: pytest.MonkeyPatch,
    ctx: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ремукс не шлёт прогресса: без отдельного статуса это выглядит зависанием."""
    task_id = await make_task(session_factory)
    stub_probe(monkeypatch)
    seen: list[str] = []

    async def download(url: str, **kwargs: Any) -> Path:
        await kwargs["on_postprocess"](ytdlp.PostProcess(status="started", processor="Merger"))
        stored = await fetch_task(session_factory, uuid.UUID(str(task_id)))
        seen.append(stored.status.value)
        destination: Path = kwargs["dest_dir"]
        destination.mkdir(parents=True, exist_ok=True)
        produced = destination / "video.mp4"
        produced.write_bytes(b"x")
        return produced

    monkeypatch.setattr(ytdlp, "download", download)

    await worker.download_task(ctx, str(task_id))

    assert seen == ["processing"]
    assert (await fetch_task(session_factory, task_id)).status is TaskStatus.READY


def _failing_probe(error: Exception) -> Any:
    async def probe(url: str, *, timeout: float) -> ytdlp.MediaInfo:
        raise error

    return probe

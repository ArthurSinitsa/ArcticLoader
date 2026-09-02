import json
import uuid
from collections.abc import Callable

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Task, TaskStatus
from app.services import tasks as tasks_repo
from app.services import url_meta
from app.services.formats import Quality
from app.services.ytdlp import MediaFormat, MediaInfo, YtDlpError
from tests.conftest import FakeArqPool

SOURCE_URL = "https://example.com/watch?v=1"

MEDIA_INFO = MediaInfo(
    title="Видео",
    extractor="Generic",
    duration=90.0,
    formats=(
        MediaFormat(format_id="a", ext="m4a", vcodec="none", acodec="mp4a", filesize=1024),
        MediaFormat(format_id="v1", ext="mp4", height=360, vcodec="avc1", acodec="none"),
        MediaFormat(format_id="v2", ext="mp4", height=720, vcodec="avc1", acodec="none"),
    ),
)


def _fake_probe(result: MediaInfo | Exception) -> Callable:
    async def probe(url: str, *, timeout: float) -> MediaInfo:
        if isinstance(result, Exception):
            raise result
        return result

    return probe


async def test_create_download_enqueues_job(client: AsyncClient, arq_pool: FakeArqPool) -> None:
    response = await client.post("/api/downloads", json={"url": SOURCE_URL})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert arq_pool.jobs == [("download_task", (body["task_id"],))]


async def test_create_download_stores_quality(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post("/api/downloads", json={"url": SOURCE_URL, "quality": "720p"})

    task_id = uuid.UUID(response.json()["task_id"])
    async with session_factory() as session:
        stored = await tasks_repo.get_task(session, task_id)

    assert stored.quality == "720p"


async def test_create_download_defaults_to_1080p(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Умолчание — не «максимум»: 4K стоит гигабайта трафика в обе стороны."""
    response = await client.post("/api/downloads", json={"url": SOURCE_URL})

    task_id = uuid.UUID(response.json()["task_id"])
    async with session_factory() as session:
        stored = await tasks_repo.get_task(session, task_id)

    assert stored.quality == "1080p"


async def test_create_download_rejects_raw_ytdlp_selector(client: AsyncClient) -> None:
    """Сырое выражение в -f от пользователя наружу не пускаем (раздел 2.7)."""
    response = await client.post(
        "/api/downloads", json={"url": SOURCE_URL, "quality": "bv*[height<=720]+ba"}
    )

    assert response.status_code == 422


async def test_create_download_rejects_invalid_url(client: AsyncClient) -> None:
    response = await client.post("/api/downloads", json={"url": "не ссылка"})

    assert response.status_code == 422


async def test_create_download_marks_task_failed_when_queue_is_down(
    client: AsyncClient,
    arq_pool: FakeArqPool,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    arq_pool.reachable = False

    response = await client.post("/api/downloads", json={"url": SOURCE_URL})

    assert response.status_code == 503
    async with session_factory() as session:
        stored = (await session.execute(sa.select(Task))).scalar_one()
    assert stored.status is TaskStatus.FAILED
    assert stored.error_code == "queue_unavailable"


async def test_get_download_returns_404_for_unknown_task(client: AsyncClient) -> None:
    response = await client.get(f"/api/downloads/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_get_download_reports_progress(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        task = await tasks_repo.create_task(session, source_url=SOURCE_URL, quality=Quality.P1080)
        await tasks_repo.set_status(
            session, task.id, TaskStatus.DOWNLOADING, progress=37.5, title="Видео"
        )

    body = (await client.get(f"/api/downloads/{task.id}")).json()

    assert body["status"] == "downloading"
    assert body["progress"] == 37.5
    assert body["title"] == "Видео"
    # Ссылка появляется только когда файл готов.
    assert body["download_url"] is None


async def test_ready_download_points_at_caddy(
    client: AsyncClient,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        task = await tasks_repo.create_task(session, source_url=SOURCE_URL, quality=Quality.P1080)
        file_path = settings.media_root / str(task.id) / "video.mp4"
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(b"x" * 2048)
        await tasks_repo.set_status(
            session,
            task.id,
            TaskStatus.READY,
            progress=100.0,
            file_path=str(file_path),
            file_size=2048,
        )

    body = (await client.get(f"/api/downloads/{task.id}")).json()

    assert body["status"] == "ready"
    assert body["file_size"] == 2048
    assert body["download_url"] == f"http://testserver/files/{task.id}/video.mp4"


async def test_direct_download_points_at_cdn(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Progressive-ветка раздела 1.5: сервер файл не качал, ссылка ведёт на CDN."""
    async with session_factory() as session:
        task = await tasks_repo.create_task(session, source_url=SOURCE_URL, quality=Quality.P360)
        await tasks_repo.set_status(
            session,
            task.id,
            TaskStatus.READY,
            progress=100.0,
            direct_url="https://cdn.example.com/video.mp4?sig=abc",
            file_size=4096,
        )

    body = (await client.get(f"/api/downloads/{task.id}")).json()

    assert body["download_url"] == "https://cdn.example.com/video.mp4?sig=abc"
    assert body["direct"] is True


async def read_sse(client: AsyncClient, url: str) -> list[dict]:
    """Читает поток до конца и возвращает разобранные события."""
    async with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])

    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


async def test_events_stream_opens_with_current_state(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Клиент не должен ждать, пока воркер соблаговолит прислать событие."""
    async with session_factory() as session:
        task = await tasks_repo.create_task(session, source_url=SOURCE_URL, quality=Quality.P720)

    payloads = await read_sse(client, f"/api/downloads/{task.id}/events")

    assert payloads[0]["status"] == "queued"
    assert payloads[0]["quality"] == "720p"


async def test_events_stream_forwards_worker_updates(
    client: AsyncClient,
    arq_pool: FakeArqPool,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        task = await tasks_repo.create_task(session, source_url=SOURCE_URL, quality=Quality.P1080)
    arq_pool.queue_event(task.id, {"status": "downloading", "progress": 42.0})

    payloads = await read_sse(client, f"/api/downloads/{task.id}/events")

    assert payloads[1] == {"status": "downloading", "progress": 42.0}


async def test_events_stream_closes_on_terminal_state(
    client: AsyncClient,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """В готовом состоянии поток отдаёт полное состояние со ссылкой и закрывается."""
    async with session_factory() as session:
        task = await tasks_repo.create_task(session, source_url=SOURCE_URL, quality=Quality.P360)
        await tasks_repo.set_status(
            session,
            task.id,
            TaskStatus.READY,
            progress=100.0,
            direct_url="https://cdn.example.com/v.mp4",
            file_size=10,
        )

    payloads = await read_sse(client, f"/api/downloads/{task.id}/events")

    assert len(payloads) == 1
    assert payloads[0]["download_url"] == "https://cdn.example.com/v.mp4"
    assert payloads[0]["direct"] is True


async def test_events_stream_404_for_unknown_task(client: AsyncClient) -> None:
    response = await client.get(f"/api/downloads/{uuid.uuid4()}/events")

    assert response.status_code == 404


async def test_failed_task_gets_human_readable_hint(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """«Не получилось» — это не сообщение об ошибке (раздел 3.4)."""
    async with session_factory() as session:
        task = await tasks_repo.create_task(session, source_url=SOURCE_URL, quality=Quality.P1080)
        await tasks_repo.set_status(
            session,
            task.id,
            TaskStatus.FAILED,
            error_code="geo_blocked",
            error_message="ERROR: not available in your country",
        )

    body = (await client.get(f"/api/downloads/{task.id}")).json()

    assert body["error_hint"] == "Видео недоступно в этой стране."
    # Сырой stderr остаётся для журнала и будущей админки.
    assert "not available" in body["error_message"]


async def test_formats_returns_available_qualities(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(url_meta, "probe", _fake_probe(MEDIA_INFO))

    body = (await client.get("/api/formats", params={"url": SOURCE_URL})).json()

    assert body["title"] == "Видео"
    assert [option["quality"] for option in body["options"]] == ["audio", "360p", "720p"]
    assert body["options"][1]["label"] == "360p"


async def test_formats_reports_extractor_failure(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        url_meta, "probe", _fake_probe(YtDlpError("unsupported_site", "Unsupported URL"))
    )

    response = await client.get("/api/formats", params={"url": SOURCE_URL})

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "unsupported_site"


async def test_formats_rejects_media_without_usable_formats(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        url_meta, "probe", _fake_probe(MediaInfo(title="Пусто", extractor="Generic"))
    )

    response = await client.get("/api/formats", params={"url": SOURCE_URL})

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "no_formats"


async def test_healthz_reports_all_components(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"db": "ok", "redis": "ok"}


async def test_healthz_fails_when_redis_is_down(client: AsyncClient, arq_pool: FakeArqPool) -> None:
    arq_pool.reachable = False

    response = await client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"db": "ok", "redis": "error"}

import uuid

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Task, TaskStatus
from app.services import tasks as tasks_repo
from tests.conftest import FakeArqPool

SOURCE_URL = "https://example.com/watch?v=1"


async def test_create_download_enqueues_job(client: AsyncClient, arq_pool: FakeArqPool) -> None:
    response = await client.post("/api/downloads", json={"url": SOURCE_URL})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert arq_pool.jobs == [("download_task", (body["task_id"],))]


async def test_create_download_passes_format(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(
        "/api/downloads", json={"url": SOURCE_URL, "format_id": "bv*[height<=720]+ba"}
    )

    task_id = uuid.UUID(response.json()["task_id"])
    async with session_factory() as session:
        stored = await tasks_repo.get_task(session, task_id)

    assert stored.format_id == "bv*[height<=720]+ba"


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
        task = await tasks_repo.create_task(session, source_url=SOURCE_URL, format_id=None)
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
        task = await tasks_repo.create_task(session, source_url=SOURCE_URL, format_id=None)
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


async def test_healthz_reports_all_components(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"db": "ok", "redis": "ok"}


async def test_healthz_fails_when_redis_is_down(client: AsyncClient, arq_pool: FakeArqPool) -> None:
    arq_pool.reachable = False

    response = await client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"db": "ok", "redis": "error"}

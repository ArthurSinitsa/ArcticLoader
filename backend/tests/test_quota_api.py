"""Квоты на входе в API — разделы 2.5.2 и 3.

Гостю по таблице 2.5.2 положены одна параллельная загрузка, пачка в один
токен и пять загрузок в сутки, поэтому все отказы проверяются на нём.
"""

from typing import Any

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Task, TaskStatus
from tests.conftest import FakeArqPool

URL = "https://example.com/video.mp4"
ANOTHER_URL = "https://example.com/second.mp4"


def _payload(url: str = URL) -> dict[str, Any]:
    return {"url": url, "quality": "720p"}


async def _finish(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Увести все задачи в терминальный статус — освободить параллельный слот."""
    async with session_factory() as session:
        await session.execute(sa.update(Task).values(status=TaskStatus.READY))
        await session.commit()


async def test_first_download_is_allowed(client: AsyncClient) -> None:
    response = await client.post("/api/downloads", json=_payload())

    assert response.status_code == 202


async def test_task_remembers_the_guest(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Без отпечатка на задаче квота гостя не с чем связать."""
    await client.post("/api/downloads", json=_payload())

    async with session_factory() as session:
        task = await session.scalar(sa.select(Task))

    assert task.guest_fingerprint
    assert task.user_id is None


async def test_second_parallel_download_is_refused(client: AsyncClient) -> None:
    """Гостю положена одна загрузка за раз, первая ещё в очереди."""
    await client.post("/api/downloads", json=_payload())

    response = await client.post("/api/downloads", json=_payload(ANOTHER_URL))

    assert response.status_code == 429
    assert response.json()["detail"]["reason"] == "concurrent"


async def test_pace_is_limited_after_previous_finished(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Слот освободился, но токен в ведре был один — упираемся в темп."""
    await client.post("/api/downloads", json=_payload())
    await _finish(session_factory)

    response = await client.post("/api/downloads", json=_payload(ANOTHER_URL))

    assert response.status_code == 429
    body = response.json()["detail"]
    assert body["reason"] == "bucket"
    assert body["retry_after"] > 0
    assert response.headers["Retry-After"] == str(body["retry_after"])


async def test_daily_limit_is_reported_separately(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    arq_pool: FakeArqPool,
) -> None:
    """Исчерпанные сутки — не то же самое, что слишком частые клики."""
    await client.post("/api/downloads", json=_payload())
    async with session_factory() as session:
        fingerprint = await session.scalar(sa.select(Task.guest_fingerprint))
    await _finish(session_factory)

    # Пятая загрузка за сутки уже сделана — счётчик выставляем напрямую,
    # чтобы не ждать восполнения ведра пять раз по полчаса.
    keys = await arq_pool.keys(f"daily:guest:{fingerprint}:*")
    await arq_pool.set(keys[0], 5)

    response = await client.post("/api/downloads", json=_payload(ANOTHER_URL))

    assert response.status_code == 429
    assert response.json()["detail"]["reason"] == "daily"


async def test_refused_download_leaves_no_task(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Отказ по квоте не должен оставлять мусор в очереди и в таблице."""
    await client.post("/api/downloads", json=_payload())

    await client.post("/api/downloads", json=_payload(ANOTHER_URL))

    async with session_factory() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(Task))
    assert count == 1


async def test_different_guests_do_not_share_quota(client: AsyncClient) -> None:
    """За общим NAT отпечаток разный — иначе офис делил бы одну квоту."""
    await client.post("/api/downloads", json=_payload(), headers={"User-Agent": "first"})

    response = await client.post(
        "/api/downloads", json=_payload(ANOTHER_URL), headers={"User-Agent": "second"}
    )

    assert response.status_code == 202


async def test_guest_access_switch_closes_anonymous_downloads(
    client: AsyncClient, settings: Settings
) -> None:
    """Рубильник раздела 3.6: волна ботов лечится флагом, а не деплоем."""
    settings.guest_access_enabled = False

    response = await client.post("/api/downloads", json=_payload())

    assert response.status_code == 403

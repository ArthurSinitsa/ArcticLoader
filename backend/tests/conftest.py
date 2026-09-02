"""Общие фикстуры.

БД — SQLite в памяти: модели написаны на переносимых типах SQLAlchemy, так что
тесты не требуют поднятого Postgres. Redis — `fakeredis` в памяти: очередь
поверх него заглушена (задача проверяется по факту постановки, а не по работе
брокера), но команды самого Redis настоящие — token bucket живёт в Lua, и на
заглушке проверялась бы заглушка.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import StaticPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import create_session_factory
from app.main import create_app
from app.models import Base
from app.services.roles import ensure_roles


class FakePubSub:
    """Подписка, отдающая заранее подготовленные события и завершающаяся."""

    def __init__(self, mailbox: dict[str, list[dict[str, Any]]]) -> None:
        self._mailbox = mailbox
        self._channel = ""
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self._channel = channel

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        for payload in self._mailbox.get(self._channel, []):
            yield {"type": "message", "data": json.dumps(payload)}

    async def aclose(self) -> None:
        self.closed = True


class FakeArqPool(FakeAsyncRedis):
    """Пул ARQ поверх Redis в памяти.

    Очередь и события — заглушки: тесты смотрят, что именно поставлено и
    опубликовано. Всё остальное (ключи квот, `EVAL`) работает по-настоящему.
    """

    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.reachable = True
        self._mailbox: dict[str, list[dict[str, Any]]] = {}

    async def enqueue_job(self, function: str, *args: Any) -> None:
        if not self.reachable:
            raise ConnectionError("redis недоступен")
        self.jobs.append((function, args))

    async def ping(self) -> bytes:
        if not self.reachable:
            raise ConnectionError("redis недоступен")
        return b"PONG"

    async def publish(self, channel: str, data: str) -> None:
        self.published.append((channel, json.loads(data)))

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self._mailbox)

    def queue_event(self, task_id: Any, payload: dict[str, Any]) -> None:
        """Подложить событие, которое увидит подписчик SSE."""
        self._mailbox.setdefault(f"task:{task_id}", []).append(payload)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        redis_url="redis://localhost:6379/0",
        media_root=tmp_path / "media",
        public_files_base_url="http://testserver/files",
    )


@pytest.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    # StaticPool держит одно соединение — иначе каждая сессия получит свою
    # пустую базу в памяти.
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield create_session_factory(engine)

    await engine.dispose()


@pytest.fixture
def arq_pool() -> FakeArqPool:
    return FakeArqPool()


@pytest.fixture
async def client(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    arq_pool: FakeArqPool,
) -> AsyncClient:
    app = create_app()
    # ASGITransport не запускает lifespan, поэтому состояние заполняем сами.
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.arq_pool = arq_pool

    # Справочник ролей в бою заводит lifespan; без него любой запрос к API
    # упёрся бы в отсутствие квот.
    async with session_factory() as session:
        await ensure_roles(session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as http_client:
        yield http_client

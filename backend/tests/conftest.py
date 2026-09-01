"""Общие фикстуры.

БД — SQLite в памяти: модели написаны на переносимых типах SQLAlchemy, так что
тесты не требуют поднятого Postgres. Redis подменяется заглушкой — очередь на
этом уровне проверяется по факту постановки задачи, а не по работе брокера.
"""

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import StaticPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import create_session_factory
from app.main import create_app
from app.models import Base


class FakeArqPool:
    """Заглушка пула ARQ: запоминает поставленные задачи."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []
        self.reachable = True

    async def enqueue_job(self, function: str, *args: Any) -> None:
        if not self.reachable:
            raise ConnectionError("redis недоступен")
        self.jobs.append((function, args))

    async def ping(self) -> bytes:
        if not self.reachable:
            raise ConnectionError("redis недоступен")
        return b"PONG"


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

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as http_client:
        yield http_client

"""Подключение к БД.

Движок создаётся явно на старте процесса (lifespan у API, `on_startup` у
воркера) и кладётся в состояние приложения — глобальных объектов нет, поэтому
в тестах подменяется одной строкой.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)

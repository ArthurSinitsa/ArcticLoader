"""Arctic Loader — точка входа API."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq.connections import RedisSettings, create_pool
from fastapi import FastAPI

from app.api import downloads, health
from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.logging_setup import configure_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url)
    app.state.settings = settings
    app.state.session_factory = create_session_factory(engine)
    app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    log.info("Arctic Loader API запущен")

    try:
        yield
    finally:
        await app.state.arq_pool.aclose()
        await engine.dispose()
        log.info("Arctic Loader API остановлен")


def create_app() -> FastAPI:
    app = FastAPI(title="Arctic Loader", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(downloads.router)
    return app


app = create_app()

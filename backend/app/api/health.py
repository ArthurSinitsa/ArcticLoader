"""Проверка живости зависимостей — для мониторинга и restart-политик Docker."""

import logging
from collections.abc import Awaitable

import sqlalchemy as sa
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.api.deps import ArqDep, SessionDep, SettingsDep
from app.schemas import PublicConfig

log = logging.getLogger(__name__)

router = APIRouter(tags=["service"])

OK = "ok"
ERROR = "error"


@router.get("/healthz")
async def healthz(session: SessionDep, arq: ArqDep) -> JSONResponse:
    components = {
        "db": await _probe("db", session.execute(sa.text("SELECT 1"))),
        "redis": await _probe("redis", arq.ping()),
    }
    healthy = all(state == OK for state in components.values())
    return JSONResponse(
        components,
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


async def _probe(name: str, check: Awaitable[object]) -> str:
    try:
        await check
    except Exception:
        log.exception("healthz: %s недоступен", name)
        return ERROR
    return OK


@router.get("/api/config")
async def public_config(settings: SettingsDep) -> PublicConfig:
    """Что странице нужно знать до входа.

    Сюда попадает только публичное: секретный ключ Turnstile остаётся на
    сервере, наружу уходит тот, что и так виден в разметке виджета.
    """
    return PublicConfig(turnstile_site_key=settings.turnstile_site_key)

"""Список доступных вариантов качества для выпадающего списка (раздел 2.7)."""

import logging
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import HttpUrl

from app.api.deps import SessionDep, SettingsDep
from app.schemas import FormatsResponse, QualityOptionOut
from app.services import formats, url_meta
from app.services.ytdlp import YtDlpError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/formats", tags=["formats"])


@router.get("")
async def list_formats(
    url: Annotated[HttpUrl, Query(description="Ссылка на видео")],
    session: SessionDep,
    settings: SettingsDep,
) -> FormatsResponse:
    try:
        info = await url_meta.get_or_fetch(
            session, str(url), ttl_seconds=settings.url_meta_ttl_seconds
        )
    except YtDlpError as exc:
        log.info("не удалось получить форматы для %s: %s", url, exc.code)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"error_code": exc.code, "message": str(exc)},
        ) from exc

    options = formats.available(info.formats)
    if not options:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"error_code": "no_formats", "message": "у ссылки нет пригодных форматов"},
        )

    return FormatsResponse(
        title=info.title,
        extractor=info.extractor,
        duration=info.duration,
        options=[QualityOptionOut(**asdict(option)) for option in options],
    )

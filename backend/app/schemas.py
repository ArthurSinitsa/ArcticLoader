"""DTO запросов и ответов API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, HttpUrl

from app.models import TaskStatus
from app.services.formats import DEFAULT_QUALITY, Quality


class DownloadCreate(BaseModel):
    url: HttpUrl
    #: Только профиль из раздела 2.7 — сырой селектор yt-dlp наружу не пускаем.
    quality: Quality = DEFAULT_QUALITY


class DownloadCreated(BaseModel):
    task_id: uuid.UUID
    status: TaskStatus


class DownloadState(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_url: str
    status: TaskStatus
    quality: Quality
    progress: float
    title: str | None
    extractor: str | None
    error_code: str | None
    #: Сырой хвост stderr — для журнала и будущей админки.
    error_message: str | None
    attempts: int
    file_size: int | None
    created_at: datetime
    finished_at: datetime | None

    #: Заполняется только в статусе `ready`. Ведёт либо на Caddy, либо прямо
    #: на CDN источника, если файл не проходил через сервер (раздел 1.5).
    download_url: str | None = None
    #: True, когда сервер в передаче файла не участвовал.
    direct: bool = False
    #: Человеческое объяснение ошибки — то, что видит пользователь.
    error_hint: str | None = None


class QualityOptionOut(BaseModel):
    quality: Quality
    label: str
    height: int | None
    filesize: int | None


class FormatsResponse(BaseModel):
    title: str
    extractor: str
    duration: float | None
    options: list[QualityOptionOut]

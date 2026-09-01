"""DTO запросов и ответов API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.models import TaskStatus


class DownloadCreate(BaseModel):
    url: HttpUrl
    format_id: str | None = Field(default=None, max_length=128)


class DownloadCreated(BaseModel):
    task_id: uuid.UUID
    status: TaskStatus


class DownloadState(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_url: str
    status: TaskStatus
    progress: float
    title: str | None
    extractor: str | None
    error_code: str | None
    error_message: str | None
    file_size: int | None
    created_at: datetime
    finished_at: datetime | None

    #: Заполняется только в статусе `ready`; ведёт на Caddy, не на приложение.
    download_url: str | None = None

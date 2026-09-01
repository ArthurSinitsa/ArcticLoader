"""Эндпоинты загрузок. API делает только быстрые операции — раздел 2.1 плана."""

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from app.api.deps import ArqDep, SessionDep, SettingsDep
from app.models import TaskStatus
from app.schemas import DownloadCreate, DownloadCreated, DownloadState
from app.services import storage
from app.services import tasks as tasks_repo

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/downloads", tags=["downloads"])

DOWNLOAD_TASK = "download_task"


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_download(
    payload: DownloadCreate, session: SessionDep, arq: ArqDep
) -> DownloadCreated:
    task = await tasks_repo.create_task(
        session, source_url=str(payload.url), format_id=payload.format_id
    )

    try:
        # Ставим в очередь только после коммита: иначе воркер может не увидеть строку.
        await arq.enqueue_job(DOWNLOAD_TASK, str(task.id))
    except Exception as exc:
        log.exception("не удалось поставить задачу %s в очередь", task.id)
        await tasks_repo.set_status(
            session,
            task.id,
            TaskStatus.FAILED,
            error_code="queue_unavailable",
            error_message=str(exc),
        )
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "очередь недоступна") from exc

    log.info("задача %s поставлена в очередь", task.id)
    return DownloadCreated(task_id=task.id, status=task.status)


@router.get("/{task_id}")
async def get_download(
    task_id: uuid.UUID, session: SessionDep, settings: SettingsDep
) -> DownloadState:
    task = await tasks_repo.get_task(session, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "задача не найдена")

    state = DownloadState.model_validate(task)
    if task.status is TaskStatus.READY and task.file_path:
        state.download_url = storage.public_url(
            settings.public_files_base_url, settings.media_root, Path(task.file_path)
        )
    return state

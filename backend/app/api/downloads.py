"""Эндпоинты загрузок. API делает только быстрые операции — раздел 2.1 плана."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.deps import ArqDep, Principal, PrincipalDep, SessionDep, SettingsDep
from app.config import Settings
from app.models import TERMINAL_STATUSES, Task, TaskStatus
from app.schemas import DownloadCreate, DownloadCreated, DownloadHistory, DownloadState
from app.services import events, quotas, storage
from app.services import tasks as tasks_repo
from app.services.ytdlp import user_message

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/downloads", tags=["downloads"])

DOWNLOAD_TASK = "download_task"

#: Комментарий раз в 15 секунд не даёт прокси закрыть простаивающее соединение.
HEARTBEAT_SECONDS = 15.0


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_download(
    payload: DownloadCreate,
    session: SessionDep,
    arq: ArqDep,
    settings: SettingsDep,
    principal: PrincipalDep,
) -> DownloadCreated:
    if principal.is_guest and not settings.guest_access_enabled:
        # Рубильник раздела 3.6: подозрительная активность лечится флагом.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "анонимное скачивание временно отключено")

    if principal.must_change_password:
        # Временный пароль знает не только владелец (раздел 2.6): до смены
        # учётка не должна ничего уметь.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "смените временный пароль, чтобы пользоваться сервисом"
        )

    await _check_quota(session, arq, principal)

    task = await tasks_repo.create_task(
        session,
        source_url=str(payload.url),
        quality=payload.quality,
        user_id=principal.user_id,
        guest_fingerprint=principal.fingerprint,
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

    log.info("задача %s поставлена в очередь, качество %s", task.id, payload.quality)
    return DownloadCreated(task_id=task.id, status=task.status)


@router.get("")
async def list_downloads(
    session: SessionDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DownloadHistory:
    """История загрузок — матрица 2.5.1.

    Гостю не положена: учётки у него нет, а связать историю с отпечатком
    значило бы показать чужие загрузки каждому, кто сидит за тем же NAT с тем
    же браузером.
    """
    if principal.user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "история доступна после входа")

    tasks, total = await tasks_repo.list_for_user(
        session, principal.user_id, limit=limit, offset=offset
    )
    return DownloadHistory(items=[build_state(task, settings) for task in tasks], total=total)


@router.get("/{task_id}")
async def get_download(
    task_id: uuid.UUID, session: SessionDep, settings: SettingsDep
) -> DownloadState:
    task = await tasks_repo.get_task(session, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "задача не найдена")
    return build_state(task, settings)


@router.get("/{task_id}/events")
async def stream_download(task_id: uuid.UUID, request: Request) -> StreamingResponse:
    """Живой прогресс через SSE.

    Односторонний поток, переподключение браузер делает сам, проходит через
    любой прокси — раздел 2.3 плана.
    """
    session_factory = request.app.state.session_factory
    settings = request.app.state.settings

    async with session_factory() as session:
        task = await tasks_repo.get_task(session, task_id)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "задача не найдена")
        initial = build_state(task, settings)

    return StreamingResponse(
        _events(request.app.state.arq_pool, session_factory, settings, task_id, initial),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def build_state(task: Task, settings: Settings) -> DownloadState:
    state = DownloadState.model_validate(task)
    state.error_hint = user_message(task.error_code)
    if task.status is TaskStatus.READY:
        state.download_url = _download_url(task, settings)
        state.direct = task.direct_url is not None
    return state


async def _events(
    arq: Any,
    session_factory: Any,
    settings: Settings,
    task_id: uuid.UUID,
    initial: DownloadState,
) -> AsyncIterator[str]:
    # Первое сообщение — текущее состояние: клиент не должен ждать, пока
    # воркер соблаговолит прислать событие.
    yield _sse(initial.model_dump(mode="json"))
    if initial.status in TERMINAL_STATUSES:
        return

    stream = events.listen(arq, task_id)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(anext(stream), HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            except StopAsyncIteration:
                return

            if payload.get("status") in TERMINAL_STATUSES:
                # В событии нет ссылки на файл: её собирает API, зная настройки.
                async with session_factory() as session:
                    task = await tasks_repo.get_task(session, task_id)
                if task is not None:
                    yield _sse(build_state(task, settings).model_dump(mode="json"))
                return

            yield _sse(payload)
    finally:
        await stream.aclose()


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def _download_url(task: Task, settings: Settings) -> str | None:
    """Прямая ссылка на CDN, если сервер файл не качал, иначе путь к Caddy."""
    if task.direct_url:
        return task.direct_url
    if not task.file_path:
        return None

    try:
        return storage.public_url(
            settings.public_files_base_url, settings.media_root, Path(task.file_path)
        )
    except ValueError:
        # Каталог загрузок переназначили, а старые записи остались: Caddy
        # такой файл всё равно не отдаст, но история из-за этого падать
        # не должна.
        log.warning("файл задачи %s лежит вне каталога загрузок: %s", task.id, task.file_path)
        return None


async def _check_quota(session: Any, arq: Any, principal: Principal) -> None:
    """Пропустить загрузку или отказать по квоте — раздел 2.5.2.

    Порядок важен: параллельный лимит проверяется первым, потому что он
    единственный не имеет побочных эффектов. Упрись мы сначала в ведро —
    токен был бы списан за загрузку, которая всё равно не началась.
    """
    quota = principal.quota
    if quota.unlimited:
        return

    active = await tasks_repo.count_active(
        session, user_id=principal.user_id, guest_fingerprint=principal.fingerprint
    )
    if active >= quota.concurrent_limit:
        raise _quota_error(
            reason="concurrent",
            retry_after=0,
            message=(
                f"Одновременно можно качать не больше {quota.concurrent_limit}. "
                "Дождитесь окончания текущей загрузки."
            ),
        )

    decision = await quotas.consume(arq, subject=principal.subject, quota=quota)
    if decision.allowed:
        return

    if decision.reason == "daily":
        message = (
            f"Суточный лимит в {quota.daily_limit} загрузок исчерпан. "
            "Следующие будут доступны после полуночи."
        )
    else:
        message = f"Слишком часто. Следующая загрузка — через {_humanize(decision.retry_after)}."

    raise _quota_error(
        reason=decision.reason or "quota",
        retry_after=decision.retry_after,
        message=message,
    )


def _quota_error(*, reason: str, retry_after: int, message: str) -> HTTPException:
    """429 с машиночитаемой причиной: UI показывает текст, а клиент-скрипт
    смотрит на `reason` и `retry_after`."""
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"reason": reason, "retry_after": retry_after, "message": message},
        headers={"Retry-After": str(retry_after)},
    )


def _humanize(seconds: int) -> str:
    if seconds >= 60:
        return f"{round(seconds / 60)} мин"
    return f"{seconds} с"

"""Заявки на регистрацию — раздел 2.6 плана.

Публичной формы signup нет и не будет: человек приходит в бота, оставляет
email и имя, а учётку заводит админ вручную. Здесь живёт всё, что происходит с
заявкой до решения; само одобрение — в админке, ему нужны и роли, и пароли.
"""

import logging
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RegistrationRequest, RequestStatus

log = logging.getLogger(__name__)


async def create(
    session: AsyncSession,
    *,
    telegram_id: int,
    telegram_username: str | None,
    email: str,
    name: str,
) -> RegistrationRequest:
    """Оставить заявку.

    Незакрытая заявка от того же человека заменяется: обычно это значит, что
    он ошибся в почте и начал заново, — двух его заявок в очереди быть не
    должно. Уже решённые не трогаем: по ним видно, кто кого впустил.
    """
    await session.execute(
        sa.delete(RegistrationRequest).where(
            RegistrationRequest.telegram_id == telegram_id,
            RegistrationRequest.status == RequestStatus.PENDING,
        )
    )

    request = RegistrationRequest(
        telegram_id=telegram_id,
        telegram_username=telegram_username,
        # Нижний регистр: иначе `Ivan@` и `ivan@` заведут две учётки на одного.
        email=email.strip().lower(),
        name=name.strip(),
        status=RequestStatus.PENDING,
    )
    session.add(request)
    await session.commit()
    await session.refresh(request)

    log.info("заявка от %s (%s)", request.email, telegram_id)
    return request


async def pending(
    session: AsyncSession, *, limit: int, offset: int = 0
) -> list[RegistrationRequest]:
    """Очередь на рассмотрение, старые первыми — их ждут дольше всех."""
    rows = await session.scalars(
        sa.select(RegistrationRequest)
        .where(RegistrationRequest.status == RequestStatus.PENDING)
        .order_by(RegistrationRequest.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows.all())


async def get(session: AsyncSession, request_id: uuid.UUID) -> RegistrationRequest | None:
    return await session.get(RegistrationRequest, request_id)


async def reject(
    session: AsyncSession, request_id: uuid.UUID, *, processed_by: uuid.UUID | None
) -> RegistrationRequest | None:
    return await _close(session, request_id, RequestStatus.REJECTED, processed_by)


async def mark_approved(
    session: AsyncSession, request_id: uuid.UUID, *, processed_by: uuid.UUID | None
) -> RegistrationRequest | None:
    return await _close(session, request_id, RequestStatus.APPROVED, processed_by)


async def mark_expired(session: AsyncSession, request_id: uuid.UUID) -> RegistrationRequest | None:
    """Одобрили, но человек не активировал пароль за отведённый срок."""
    return await _close(session, request_id, RequestStatus.EXPIRED, None)


async def _close(
    session: AsyncSession,
    request_id: uuid.UUID,
    status: RequestStatus,
    processed_by: uuid.UUID | None,
) -> RegistrationRequest | None:
    request = await session.get(RegistrationRequest, request_id)
    if request is None:
        return None

    request.status = status
    request.processed_by = processed_by
    request.processed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(request)

    log.info("заявка %s: %s", request.email, status.value)
    return request

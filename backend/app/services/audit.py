"""Журнал административных действий — раздел 3.6 плана.

Пишется на каждое действие, меняющее чужие данные: удаление учётки,
прерывание чужой задачи, правка квот, переключение рубильника. Когда что-то
исчезнет, единственный способ понять причину — этот журнал.
"""

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEntry

log = logging.getLogger(__name__)


async def record(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Записать действие. `actor_id` пуст, когда действовал сам сервис."""
    session.add(
        AuditEntry(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
            ip=ip,
        )
    )
    await session.commit()
    log.info("аудит: %s над %s:%s", action, target_type, target_id)


async def recent(session: AsyncSession, *, limit: int, offset: int = 0) -> list[AuditEntry]:
    """Свежие записи первыми: журнал читают, когда что-то уже случилось."""
    # Сортируем по номеру, а не по времени: записи одной секунды иначе
    # выстраиваются в произвольном порядке.
    rows = await session.scalars(
        sa.select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit).offset(offset)
    )
    return list(rows.all())

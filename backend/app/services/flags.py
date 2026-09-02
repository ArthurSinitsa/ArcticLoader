"""Глобальные рубильники — раздел 3.6 плана.

Флаги живут в БД, а не в `.env`: подозрительная активность должна лечиться
галочкой в админке, а не срочным деплоем ночью. Строка заводится в момент
первого переключения — отсутствие записи означает значение по умолчанию.
"""

import logging
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuntimeFlag

log = logging.getLogger(__name__)

#: Анонимное скачивание. Прямой ответ на риск раздела 4.5.
GUEST_ACCESS = "guest_access_enabled"
#: Приём заявок на регистрацию (Этап 6).
REGISTRATION = "registration_enabled"
#: Очередь дорабатывает начатое, новые задачи не берутся.
MAINTENANCE = "maintenance_mode"

#: Список закрыт: иначе в таблицу натечёт мусор из опечаток в запросах.
DEFAULTS: dict[str, bool] = {
    GUEST_ACCESS: True,
    REGISTRATION: True,
    MAINTENANCE: False,
}


async def current(session: AsyncSession) -> dict[str, bool]:
    """Состояние всех рубильников. Пустая таблица — рабочее состояние."""
    rows = (await session.execute(sa.select(RuntimeFlag.key, RuntimeFlag.enabled))).all()
    stored = dict(rows)
    return {key: stored.get(key, default) for key, default in DEFAULTS.items()}


async def is_on(session: AsyncSession, key: str) -> bool:
    stored = await session.scalar(sa.select(RuntimeFlag.enabled).where(RuntimeFlag.key == key))
    return DEFAULTS[key] if stored is None else stored


async def set_flag(session: AsyncSession, key: str, enabled: bool) -> None:
    if key not in DEFAULTS:
        raise LookupError(f"нет такого рубильника: {key}")

    existing = await session.get(RuntimeFlag, key)
    if existing is None:
        session.add(RuntimeFlag(key=key, enabled=enabled))
    else:
        existing.enabled = enabled
        existing.updated_at = datetime.now(UTC)

    await session.commit()
    log.warning("рубильник %s переключён в %s", key, enabled)

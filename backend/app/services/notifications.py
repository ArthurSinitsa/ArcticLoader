"""Очередь сообщений, которые бот должен отправить, — Этап 6 плана.

Список Redis, а не pub/sub. У pub/sub нет памяти: уведомление о готовом файле
пропало бы, окажись бот в этот момент на перезапуске, — а файл уже скачан, и
человек о нём так и не узнает. Список ждёт получателя сколько нужно.

Отправляет только процесс бота: единственная точка общения с Telegram проще
следит за лимитами, чем три процесса вразнобой.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import User

log = logging.getLogger(__name__)

QUEUE = "bot:outbox"

#: Пауза ожидания в BRPOP. Нужна не для скорости, а чтобы цикл мог заметить
#: сигнал остановки, а не висел в блокирующем чтении вечно.
POP_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class Notification:
    chat_id: int
    text: str


async def publish(redis: Redis, *, chat_id: int, text: str) -> None:
    """Поставить сообщение в очередь на отправку."""
    payload = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False)
    # LPUSH и BRPOP с разных концов дают порядок «первым пришёл — первым ушёл».
    await redis.lpush(QUEUE, payload)


async def listen(
    redis: Redis, *, timeout: int = POP_TIMEOUT_SECONDS
) -> AsyncIterator[Notification]:
    """Поток сообщений на отправку. Ждёт, пока они появятся."""
    while True:
        item = await redis.brpop([QUEUE], timeout=timeout)  # type: ignore[misc]
        if item is None:
            continue

        _, raw = item
        try:
            payload = json.loads(raw)
            yield Notification(chat_id=int(payload["chat_id"]), text=str(payload["text"]))
        except json.JSONDecodeError, KeyError, TypeError, ValueError:
            # Мусор пропускаем: он не должен запирать всё, что стоит за ним.
            log.warning("в очереди уведомлений неразбираемая запись")


async def notify_owner(
    session: AsyncSession, redis: Redis, *, user_id: uuid.UUID | None, text: str
) -> bool:
    """Написать владельцу задачи, если у него привязан Telegram.

    У гостя учётки нет вовсе, а у заведённой админом вручную может не быть
    привязки — в обоих случаях уведомлять некого, и это не ошибка.
    """
    if user_id is None:
        return False

    chat_id = await session.scalar(sa.select(User.telegram_id).where(User.id == user_id))
    if not chat_id:
        return False

    await publish(redis, chat_id=chat_id, text=text)
    return True


async def alert_admins(redis: Redis, settings: Settings, text: str) -> bool:
    """Единый канал алертов — раздел 3.6.

    Пустой чат в настройках это осознанный выбор, а не поломка: заявки всё
    равно видны в админке, просто не мгновенно.
    """
    if not settings.telegram_alert_chat_id:
        return False

    await publish(redis, chat_id=settings.telegram_alert_chat_id, text=text)
    return True

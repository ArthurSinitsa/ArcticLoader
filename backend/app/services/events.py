"""Канал прогресса: воркер публикует, SSE-эндпоинт слушает (раздел 2.2).

Pub/sub, а не опрос БД: воркеров несколько, а подписчик задачи обычно один, и
каждый его тик иначе превращался бы в запрос к Postgres.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis

log = logging.getLogger(__name__)

CHANNEL_PREFIX = "task"


def channel(task_id: uuid.UUID | str) -> str:
    return f"{CHANNEL_PREFIX}:{task_id}"


async def publish(redis: Redis, task_id: uuid.UUID | str, payload: dict[str, Any]) -> None:
    """Отправить событие подписчикам. Отсутствие слушателей — норма."""
    await redis.publish(channel(task_id), json.dumps(payload, ensure_ascii=False))


async def listen(redis: Redis, task_id: uuid.UUID | str) -> AsyncIterator[dict[str, Any]]:
    """События задачи. Генератор завершается, когда отписываются."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel(task_id))
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                yield json.loads(message["data"])
            except json.JSONDecodeError:
                log.warning("в канале %s мусор вместо JSON", channel(task_id))
    finally:
        await pubsub.aclose()


#: Команда воркеру остановить задачу. Канал один на всех: процесс живёт в том
#: воркере, который его запустил, а кто именно — API не знает.
CANCEL_CHANNEL = "task:cancel"


async def request_cancel(redis: Redis, task_id: uuid.UUID | str) -> None:
    """Попросить воркер прервать задачу — раздел 2.5.3.

    API не может убить процесс сам: yt-dlp запущен в другом контейнере, и его
    PID за пределами воркера ничего не значит.
    """
    await redis.publish(CANCEL_CHANNEL, json.dumps({"task_id": str(task_id)}))


async def listen_cancellations(redis: Redis) -> AsyncIterator[uuid.UUID]:
    """Поток команд на прерывание. Чужие задачи воркер просто не найдёт у себя."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(CANCEL_CHANNEL)
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                yield uuid.UUID(json.loads(message["data"])["task_id"])
            except json.JSONDecodeError, KeyError, ValueError:
                log.warning("в канале отмен мусор вместо идентификатора задачи")
    finally:
        await pubsub.aclose()

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

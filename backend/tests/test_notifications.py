"""Очередь сообщений боту — Этап 6.

Список Redis, а не pub/sub: у pub/sub нет памяти, и уведомление о готовом
файле пропало бы, если бот в этот момент перезапускался. Файл при этом уже
скачан — человек так и не узнает.
"""

import pytest
from fakeredis import FakeAsyncRedis

from app.services import notifications

CHAT = 777001


@pytest.fixture
async def redis() -> FakeAsyncRedis:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


async def first(redis: FakeAsyncRedis) -> notifications.Notification:
    stream = notifications.listen(redis, timeout=1)
    try:
        return await anext(stream)
    finally:
        await stream.aclose()


async def test_message_reaches_the_listener(redis: FakeAsyncRedis) -> None:
    await notifications.publish(redis, chat_id=CHAT, text="файл готов")

    message = await first(redis)

    assert message.chat_id == CHAT
    assert message.text == "файл готов"


async def test_message_waits_for_a_restarted_bot(redis: FakeAsyncRedis) -> None:
    """Главная причина списка вместо pub/sub: сообщение не должно пропасть."""
    await notifications.publish(redis, chat_id=CHAT, text="первое")
    await notifications.publish(redis, chat_id=CHAT, text="второе")

    # Слушатель появляется только сейчас — оба сообщения на месте.
    stream = notifications.listen(redis, timeout=1)
    texts = [(await anext(stream)).text, (await anext(stream)).text]
    await stream.aclose()

    assert texts == ["первое", "второе"]


async def test_broken_entry_does_not_stop_the_queue(redis: FakeAsyncRedis) -> None:
    """Мусор в очереди не должен запирать всё, что за ним."""
    await redis.lpush(notifications.QUEUE, b"not json")
    await notifications.publish(redis, chat_id=CHAT, text="нужное")

    message = await first(redis)

    assert message.text == "нужное"

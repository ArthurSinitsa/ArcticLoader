"""Сессии в Redis — раздел 4.4 плана.

Сессионные cookie для веба, JWT отложен до нативных клиентов. Сессия живёт в
Redis, а не в подписанном токене: отозвать выданный JWT нечем, а выкинуть
пользователя нужно уметь — при смене пароля и при удалении учётки.
"""

import uuid

import pytest
from fakeredis import FakeAsyncRedis

from app.services import sessions

TTL = 3600


@pytest.fixture
async def redis() -> FakeAsyncRedis:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


async def test_session_resolves_to_its_owner(redis: FakeAsyncRedis) -> None:
    user_id = uuid.uuid4()

    token = await sessions.create(redis, user_id, ttl=TTL)

    assert await sessions.resolve(redis, token, ttl=TTL) == user_id


async def test_unknown_token_resolves_to_nobody(redis: FakeAsyncRedis) -> None:
    assert await sessions.resolve(redis, "выдуманный", ttl=TTL) is None


async def test_tokens_are_not_guessable(redis: FakeAsyncRedis) -> None:
    """Токен — единственное, что отделяет чужую учётку от нашей."""
    user_id = uuid.uuid4()

    first = await sessions.create(redis, user_id, ttl=TTL)
    second = await sessions.create(redis, user_id, ttl=TTL)

    assert first != second
    assert len(first) >= 32


async def test_logout_kills_the_session(redis: FakeAsyncRedis) -> None:
    user_id = uuid.uuid4()
    token = await sessions.create(redis, user_id, ttl=TTL)

    await sessions.destroy(redis, token)

    assert await sessions.resolve(redis, token, ttl=TTL) is None


async def test_session_expires(redis: FakeAsyncRedis) -> None:
    user_id = uuid.uuid4()

    token = await sessions.create(redis, user_id, ttl=TTL)

    assert 0 < await redis.ttl(f"{sessions.SESSION_PREFIX}:{token}") <= TTL


async def test_activity_prolongs_the_session(redis: FakeAsyncRedis) -> None:
    """Иначе человека выкидывает посреди работы ровно через неделю."""
    user_id = uuid.uuid4()
    token = await sessions.create(redis, user_id, ttl=TTL)
    await redis.expire(f"{sessions.SESSION_PREFIX}:{token}", 10)

    await sessions.resolve(redis, token, ttl=TTL)

    assert await redis.ttl(f"{sessions.SESSION_PREFIX}:{token}") > 10


async def test_password_change_logs_out_every_device(redis: FakeAsyncRedis) -> None:
    """Смена пароля обязана обрывать украденную сессию — иначе она бессмысленна."""
    user_id = uuid.uuid4()
    phone = await sessions.create(redis, user_id, ttl=TTL)
    laptop = await sessions.create(redis, user_id, ttl=TTL)

    await sessions.destroy_all(redis, user_id)

    assert await sessions.resolve(redis, phone, ttl=TTL) is None
    assert await sessions.resolve(redis, laptop, ttl=TTL) is None


async def test_one_users_logout_does_not_touch_another(redis: FakeAsyncRedis) -> None:
    mine = await sessions.create(redis, uuid.uuid4(), ttl=TTL)
    other_id = uuid.uuid4()
    theirs = await sessions.create(redis, other_id, ttl=TTL)

    await sessions.destroy_all(redis, other_id)

    assert await sessions.resolve(redis, mine, ttl=TTL) is not None
    assert await sessions.resolve(redis, theirs, ttl=TTL) is None


async def test_destroyed_session_leaves_no_keys(redis: FakeAsyncRedis) -> None:
    """Список сессий пользователя не должен копить мёртвые токены."""
    user_id = uuid.uuid4()
    token = await sessions.create(redis, user_id, ttl=TTL)

    await sessions.destroy(redis, token)

    assert await redis.keys("*") == []

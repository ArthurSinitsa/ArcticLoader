"""Сессии в Redis — раздел 4.4 плана.

Для веба сессионные cookie, JWT отложен до нативных клиентов. Состояние
держим на сервере, а не в подписанном токене: выданный JWT нечем отозвать, а
выкидывать пользователя нужно уметь — при смене пароля и при удалении учётки.

Второй ключ, `user_sessions:{id}`, существует ровно ради этого: без списка
токенов пользователя пришлось бы перебирать всю базу Redis.
"""

import logging
import secrets
import uuid

from redis.asyncio import Redis

log = logging.getLogger(__name__)

SESSION_PREFIX = "session"
USER_SESSIONS_PREFIX = "user_sessions"

#: 32 байта энтропии: токен — единственное, что отделяет чужую учётку от нашей.
TOKEN_BYTES = 32


def _session_key(token: str) -> str:
    return f"{SESSION_PREFIX}:{token}"


def _user_key(user_id: uuid.UUID) -> str:
    return f"{USER_SESSIONS_PREFIX}:{user_id}"


async def create(redis: Redis, user_id: uuid.UUID, *, ttl: int) -> str:
    token = secrets.token_urlsafe(TOKEN_BYTES)

    pipe = redis.pipeline()
    pipe.set(_session_key(token), str(user_id), ex=ttl)
    pipe.sadd(_user_key(user_id), token)
    # Список переживает свои сессии на сутки — на случай, если последняя
    # сессия истекла сама и вычеркнуть её было некому.
    pipe.expire(_user_key(user_id), ttl + 86400)
    await pipe.execute()

    log.info("создана сессия пользователя %s", user_id)
    return token


async def resolve(redis: Redis, token: str, *, ttl: int) -> uuid.UUID | None:
    """Владелец сессии; попутно продлевает её.

    Без продления человека выбрасывало бы ровно через неделю посреди работы,
    даже если он всё это время пользовался сервисом.
    """
    raw = await redis.get(_session_key(token))
    if raw is None:
        return None

    await redis.expire(_session_key(token), ttl)
    return uuid.UUID(raw.decode() if isinstance(raw, bytes) else raw)


async def destroy(redis: Redis, token: str) -> None:
    """Выход с одного устройства."""
    raw = await redis.get(_session_key(token))
    pipe = redis.pipeline()
    pipe.delete(_session_key(token))
    if raw is not None:
        user_id = raw.decode() if isinstance(raw, bytes) else raw
        pipe.srem(_user_key(uuid.UUID(user_id)), token)
    await pipe.execute()


async def destroy_all(redis: Redis, user_id: uuid.UUID) -> int:
    """Выход со всех устройств: смена пароля обязана обрывать чужую сессию,
    иначе смена ничего не даёт укравшему cookie."""
    tokens = await redis.smembers(_user_key(user_id))
    keys = [_session_key(t.decode() if isinstance(t, bytes) else t) for t in tokens]

    pipe = redis.pipeline()
    if keys:
        pipe.delete(*keys)
    pipe.delete(_user_key(user_id))
    await pipe.execute()

    if keys:
        log.info("сброшены все сессии пользователя %s: %s", user_id, len(keys))
    return len(keys)

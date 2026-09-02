"""Квоты: token bucket в Redis — раздел 2.5.2 плана.

Одна механика на все роли: гость, пользователь и админ отличаются только
числами из `quota_profiles`, а суперадмин обходит проверку по флагу
`is_unlimited`. Цифры живут в БД, а не в коде, — правка лимитов не должна
требовать пересборки (раздел 6).

Арифметика ведра целиком в Lua: между чтением остатка и его записью не должно
быть окна, в которое пролезет вторая загрузка того же пользователя.
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

log = logging.getLogger(__name__)

BUCKET_PREFIX = "bucket"
DAILY_PREFIX = "daily"

#: Забирает токен, если он есть, и сообщает, сколько ждать, если нет.
#:
#: Суточный счётчик и ведро проверяются здесь же, одним куском: разнеси их по
#: двум запросам — и отказ второй проверки придётся компенсировать откатом
#: первой, а откат по дороге теряется.
#:
#: KEYS[1] — ведро, KEYS[2] — суточный счётчик.
#: ARGV: ёмкость, интервал восполнения, текущее время, суточный лимит,
#: секунды до полуночи.
_CONSUME_SCRIPT = """
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local daily_limit = tonumber(ARGV[4])
local until_midnight = tonumber(ARGV[5])

-- Суточный лимит идёт первым и до списания токена: ведро отвечает за темп,
-- а исчерпанные сутки темпом не лечатся.
local used = tonumber(redis.call('GET', KEYS[2]) or '0')
if used >= daily_limit then
    return {0, until_midnight, 'daily'}
end

local state = redis.call('HMGET', KEYS[1], 'tokens', 'updated')
local tokens = tonumber(state[1])
local updated = tonumber(state[2])

if tokens == nil or updated == nil then
    tokens = capacity
    updated = now
end

-- Восполняем целыми токенами, остаток времени оставляем в `updated`:
-- иначе частые обращения обнуляли бы недокапавший токен.
local gained = math.floor((now - updated) / refill)
if gained > 0 then
    tokens = math.min(capacity, tokens + gained)
    updated = updated + gained * refill
end

-- У полного ведра время стоит: иначе простой копился бы как долг и первая же
-- загрузка после паузы отдавала бы токен «задним числом».
if tokens >= capacity then
    updated = now
end

if tokens < 1 then
    return {0, math.ceil(updated + refill - now), 'bucket'}
end

tokens = tokens - 1
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated', updated)
-- Ведро полностью восстановится за capacity интервалов; после этого запись
-- неотличима от отсутствующей, и держать её незачем.
redis.call('EXPIRE', KEYS[1], math.ceil(capacity * refill))

-- Суточный счётчик растёт только вместе с выданным токеном.
if redis.call('INCR', KEYS[2]) == 1 then
    redis.call('EXPIRE', KEYS[2], until_midnight)
end
return {1, 0, 'ok'}
"""


@dataclass(frozen=True)
class Quota:
    """Строка `quota_profiles` для одной роли."""

    daily_limit: int
    concurrent_limit: int
    bucket_capacity: int
    bucket_refill_minutes: int
    #: Флаг роли (`roles.is_unlimited`), а не квоты: суперадмин не считается
    #: вовсе. Решение принимается здесь, чтобы его нельзя было забыть на
    #: вызывающей стороне.
    unlimited: bool = False


@dataclass(frozen=True)
class Decision:
    """Ответ на попытку занять слот под загрузку."""

    allowed: bool
    #: Что именно упёрлось: `bucket` — темп, `daily` — суточный лимит.
    reason: str | None = None
    #: Через сколько секунд имеет смысл повторить.
    retry_after: int = 0


async def consume(
    redis: Redis, *, subject: str, quota: Quota, now: float | None = None
) -> Decision:
    """Занять один слот под загрузку.

    `subject` — `user:{id}` для авторизованных, `guest:{fingerprint}` для
    гостей. Время передаётся снаружи, чтобы поведение ведра можно было
    проверить тестом, а не ожиданием в полчаса.
    """
    if quota.unlimited:
        return Decision(allowed=True)

    moment = time.time() if now is None else now
    allowed, retry_after, reason = await redis.eval(  # type: ignore[misc]
        _CONSUME_SCRIPT,
        2,
        f"{BUCKET_PREFIX}:{subject}",
        _daily_key(subject, moment),
        quota.bucket_capacity,
        quota.bucket_refill_minutes * 60,
        moment,
        quota.daily_limit,
        _seconds_to_midnight(moment),
    )

    if allowed:
        return Decision(allowed=True)

    limit = reason.decode() if isinstance(reason, bytes) else str(reason)
    log.info("%s упёрся в лимит %s, ждать %s с", subject, limit, retry_after)
    return Decision(allowed=False, reason=limit, retry_after=int(retry_after))


def _daily_key(subject: str, moment: float) -> str:
    """Ключ суточного счётчика. Дата в имени — чтобы новые сутки начинались
    с чистой записи даже если старая почему-то пережила свой TTL."""
    day = datetime.fromtimestamp(moment, UTC).date()
    return f"{DAILY_PREFIX}:{subject}:{day.isoformat()}"


def _seconds_to_midnight(moment: float) -> int:
    """Сколько ждать до обнуления суток. Считаем по UTC — по нему же живут
    контейнеры, а привязка к часовому поясу пользователя дала бы разным
    людям разные сутки на одном счётчике."""
    point = datetime.fromtimestamp(moment, UTC)
    midnight = (point + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - point).total_seconds())

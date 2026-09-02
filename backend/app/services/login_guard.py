"""Защита входа от перебора — раздел 2.6 плана.

Пять неудач по связке «IP + логин» закрывают вход на пятнадцать минут.
Открытая форма входа на публичном адресе живёт до первого сканера.

Именно связка, а не одна её половина: по IP за общим NAT неудачи соседа
блокировали бы весь офис, а по одному логину любой желающий закрывал бы вход
владельцу учётки, зная только его email.
"""

import hashlib
import logging

from redis.asyncio import Redis

log = logging.getLogger(__name__)

ATTEMPTS_PREFIX = "login_fail"


def attempts_key(*, ip: str, login: str) -> str:
    """Ключ счётчика. Логин приводится к нижнему регистру: иначе перебор идёт
    по `Root@`, `rOot@` и так далее, ни разу не упираясь в блокировку."""
    pair = hashlib.sha256(f"{ip}|{login.strip().lower()}".encode()).hexdigest()
    return f"{ATTEMPTS_PREFIX}:{pair}"


async def blocked_for(redis: Redis, *, ip: str, login: str, max_attempts: int) -> int:
    """Сколько секунд вход закрыт. Ноль — можно пробовать."""
    key = attempts_key(ip=ip, login=login)
    failures = await redis.get(key)
    if failures is None or int(failures) < max_attempts:
        return 0

    # TTL ключа и есть остаток блокировки.
    return max(await redis.ttl(key), 0)


async def register_failure(
    redis: Redis, *, ip: str, login: str, max_attempts: int, block_seconds: int
) -> int:
    """Учесть неудачную попытку и вернуть их общее число."""
    key = attempts_key(ip=ip, login=login)
    failures = await redis.incr(key)

    if failures == 1 or failures >= max_attempts:
        # Первая неудача открывает окно, а последняя отсчитывает блокировку
        # заново — от неё, а не от начала серии.
        await redis.expire(key, block_seconds)

    if failures >= max_attempts:
        log.warning("вход закрыт на %s с: %s неудач подряд", block_seconds, failures)
    return failures


async def reset(redis: Redis, *, ip: str, login: str) -> None:
    """Успешный вход обнуляет счётчик: вчерашние опечатки не должны копиться
    до блокировки на ровном месте."""
    await redis.delete(attempts_key(ip=ip, login=login))

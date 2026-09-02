"""Защита входа от перебора — раздел 2.6 плана.

Пять неудач по связке «IP + логин» закрывают вход на пятнадцать минут.
Связка, а не один IP: за общим NAT неудачи соседа блокировали бы всех, а по
одному логину любой желающий закрывал бы вход владельцу учётки.
"""

import pytest
from fakeredis import FakeAsyncRedis

from app.services import login_guard

IP = "203.0.113.7"
LOGIN = "root@example.com"
MAX_ATTEMPTS = 5
BLOCK = 900


@pytest.fixture
async def redis() -> FakeAsyncRedis:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


async def _fail(redis: FakeAsyncRedis, times: int, *, ip: str = IP, login: str = LOGIN) -> None:
    for _ in range(times):
        await login_guard.register_failure(
            redis, ip=ip, login=login, max_attempts=MAX_ATTEMPTS, block_seconds=BLOCK
        )


async def _blocked_for(redis: FakeAsyncRedis, *, ip: str = IP, login: str = LOGIN) -> int:
    return await login_guard.blocked_for(redis, ip=ip, login=login, max_attempts=MAX_ATTEMPTS)


async def test_clean_pair_is_not_blocked(redis: FakeAsyncRedis) -> None:
    assert await _blocked_for(redis) == 0


async def test_four_failures_still_let_you_in(redis: FakeAsyncRedis) -> None:
    """Опечатка в пароле не должна стоить пятнадцати минут."""
    await _fail(redis, MAX_ATTEMPTS - 1)

    assert await _blocked_for(redis) == 0


async def test_fifth_failure_closes_the_door(redis: FakeAsyncRedis) -> None:
    await _fail(redis, MAX_ATTEMPTS)

    assert 0 < await _blocked_for(redis) <= BLOCK


async def test_successful_login_clears_the_counter(redis: FakeAsyncRedis) -> None:
    """Иначе вчерашние опечатки копились бы до блокировки на ровном месте."""
    await _fail(redis, MAX_ATTEMPTS - 1)

    await login_guard.reset(redis, ip=IP, login=LOGIN)
    await _fail(redis, MAX_ATTEMPTS - 1)

    assert await _blocked_for(redis) == 0


async def test_another_login_from_same_ip_is_free(redis: FakeAsyncRedis) -> None:
    """За общим NAT чужие неудачи не должны закрывать вход соседу."""
    await _fail(redis, MAX_ATTEMPTS)

    assert await _blocked_for(redis, login="someone@example.com") == 0


async def test_same_login_from_another_ip_is_free(redis: FakeAsyncRedis) -> None:
    """Иначе достаточно знать чужой email, чтобы закрыть человеку вход."""
    await _fail(redis, MAX_ATTEMPTS)

    assert await _blocked_for(redis, ip="198.51.100.4") == 0


async def test_login_case_does_not_open_a_new_counter(redis: FakeAsyncRedis) -> None:
    """Иначе перебор идёт по Root@, rOot@ и так далее без единой блокировки."""
    await _fail(redis, MAX_ATTEMPTS, login=LOGIN.upper())

    assert await _blocked_for(redis) > 0


async def test_block_is_released_on_its_own(redis: FakeAsyncRedis) -> None:
    await _fail(redis, MAX_ATTEMPTS)
    key = login_guard.attempts_key(ip=IP, login=LOGIN)

    # Имитируем истечение пятнадцати минут, не дожидаясь их.
    await redis.delete(key)

    assert await _blocked_for(redis) == 0

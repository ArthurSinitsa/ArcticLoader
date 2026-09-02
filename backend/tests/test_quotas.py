"""Token bucket и суточный счётчик — раздел 2.5.2 плана.

Redis настоящий, но в памяти: вся арифметика ведра живёт в Lua-скрипте, и
проверять её на заглушке бессмысленно — проверялась бы заглушка.
Время передаётся снаружи, иначе тест про восполнение пришлось бы ждать
двадцать минут.
"""

import pytest
from fakeredis import FakeAsyncRedis

from app.services.quotas import Quota, consume

#: Гость из таблицы 2.5.2: пачка в один токен, восполнение раз в полчаса.
GUEST = Quota(daily_limit=5, concurrent_limit=1, bucket_capacity=1, bucket_refill_minutes=30)
#: Пользователь: пачка в четыре загрузки, восполнение раз в двадцать минут.
USER = Quota(daily_limit=16, concurrent_limit=4, bucket_capacity=4, bucket_refill_minutes=20)

HOUR = 3600.0


@pytest.fixture
async def redis() -> FakeAsyncRedis:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


async def test_first_request_is_allowed(redis: FakeAsyncRedis) -> None:
    decision = await consume(redis, subject="guest:abc", quota=GUEST, now=HOUR)

    assert decision.allowed
    assert decision.reason is None


async def test_burst_is_limited_by_capacity(redis: FakeAsyncRedis) -> None:
    """Пачка гостя — один токен: вторая загрузка подряд не проходит."""
    await consume(redis, subject="guest:abc", quota=GUEST, now=HOUR)

    decision = await consume(redis, subject="guest:abc", quota=GUEST, now=HOUR)

    assert not decision.allowed
    assert decision.reason == "bucket"


async def test_capacity_allows_whole_burst(redis: FakeAsyncRedis) -> None:
    for _ in range(USER.bucket_capacity):
        assert (await consume(redis, subject="user:1", quota=USER, now=HOUR)).allowed

    assert not (await consume(redis, subject="user:1", quota=USER, now=HOUR)).allowed


async def test_token_returns_after_refill_interval(redis: FakeAsyncRedis) -> None:
    await consume(redis, subject="guest:abc", quota=GUEST, now=HOUR)

    decision = await consume(redis, subject="guest:abc", quota=GUEST, now=HOUR + 30 * 60)

    assert decision.allowed


async def test_bucket_never_overfills(redis: FakeAsyncRedis) -> None:
    """Сутки простоя не дают гостю накопить пачку больше ёмкости."""
    await consume(redis, subject="guest:abc", quota=GUEST, now=HOUR)

    assert (await consume(redis, subject="guest:abc", quota=GUEST, now=HOUR + 24 * HOUR)).allowed
    assert not (
        await consume(redis, subject="guest:abc", quota=GUEST, now=HOUR + 24 * HOUR)
    ).allowed


async def test_refusal_says_how_long_to_wait(redis: FakeAsyncRedis) -> None:
    await consume(redis, subject="guest:abc", quota=GUEST, now=HOUR)

    decision = await consume(redis, subject="guest:abc", quota=GUEST, now=HOUR + 600)

    # Токен восполняется через 30 минут, 10 из них уже прошли.
    assert decision.retry_after == 20 * 60


async def test_subjects_do_not_share_bucket(redis: FakeAsyncRedis) -> None:
    await consume(redis, subject="guest:abc", quota=GUEST, now=HOUR)

    assert (await consume(redis, subject="guest:xyz", quota=GUEST, now=HOUR)).allowed


async def test_daily_limit_stops_further_downloads(redis: FakeAsyncRedis) -> None:
    """Гостю положено пять загрузок в сутки, темп при этом соблюдён."""
    moment = HOUR
    for _ in range(GUEST.daily_limit):
        assert (await consume(redis, subject="guest:abc", quota=GUEST, now=moment)).allowed
        moment += GUEST.bucket_refill_minutes * 60

    decision = await consume(redis, subject="guest:abc", quota=GUEST, now=moment)

    assert not decision.allowed
    assert decision.reason == "daily"


async def test_rejected_burst_does_not_spend_daily_limit(redis: FakeAsyncRedis) -> None:
    """Отказ по темпу не должен списывать суточную квоту.

    Иначе пользователь, кликнувший дважды, теряет вторую загрузку за сутки,
    так её и не получив.
    """
    quota = Quota(daily_limit=2, concurrent_limit=1, bucket_capacity=1, bucket_refill_minutes=30)

    assert (await consume(redis, subject="guest:abc", quota=quota, now=HOUR)).allowed
    assert not (await consume(redis, subject="guest:abc", quota=quota, now=HOUR)).allowed

    # Третья попытка — вторая по счёту удачная, суточный лимит ещё не выбран.
    assert (await consume(redis, subject="guest:abc", quota=quota, now=HOUR + 1800)).allowed


async def test_daily_limit_resets_next_day(redis: FakeAsyncRedis) -> None:
    quota = Quota(daily_limit=1, concurrent_limit=1, bucket_capacity=1, bucket_refill_minutes=30)

    assert (await consume(redis, subject="guest:abc", quota=quota, now=HOUR)).allowed
    assert not (await consume(redis, subject="guest:abc", quota=quota, now=HOUR + 1800)).allowed

    assert (await consume(redis, subject="guest:abc", quota=quota, now=HOUR + 24 * HOUR)).allowed


async def test_daily_refusal_waits_until_midnight(redis: FakeAsyncRedis) -> None:
    quota = Quota(daily_limit=1, concurrent_limit=1, bucket_capacity=1, bucket_refill_minutes=30)
    # 1970-01-01 22:00 UTC — до полуночи два часа.
    evening = 22 * HOUR
    await consume(redis, subject="guest:abc", quota=quota, now=evening)

    decision = await consume(redis, subject="guest:abc", quota=quota, now=evening + 1800)

    assert decision.reason == "daily"
    assert decision.retry_after == int(1.5 * HOUR)


async def test_unlimited_role_is_never_refused(redis: FakeAsyncRedis) -> None:
    """Суперадмин обходит проверку по флагу is_unlimited, а не особыми числами."""
    unlimited = Quota(
        daily_limit=0,
        concurrent_limit=0,
        bucket_capacity=0,
        bucket_refill_minutes=1,
        unlimited=True,
    )

    for _ in range(10):
        assert (await consume(redis, subject="user:root", quota=unlimited, now=HOUR)).allowed

    # Безлимитная роль не должна оставлять в Redis ни одного ключа.
    assert await redis.keys("*") == []

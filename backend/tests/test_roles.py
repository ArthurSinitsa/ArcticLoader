"""Справочник ролей и квот — таблица 2.5.2 плана.

Числа лимитов проверяются здесь ровно потому, что они не должны жить в коде,
который их применяет: `quotas.consume` обязан получать их из БД.
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import QuotaProfile, Role
from app.services.roles import RoleCode, ensure_roles, load_quota


async def test_ensure_roles_creates_the_four_roles(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await ensure_roles(session)

        codes = (await session.scalars(sa.select(Role.code).order_by(Role.id))).all()

    assert list(codes) == [code.value for code in RoleCode]


async def test_ensure_roles_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await ensure_roles(session)
        await ensure_roles(session)

        count = await session.scalar(sa.select(sa.func.count()).select_from(Role))

    assert count == len(RoleCode)


async def test_second_run_keeps_edited_quotas(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правка лимитов из админки не должна откатываться перезапуском API."""
    async with session_factory() as session:
        await ensure_roles(session)
        await session.execute(
            sa.update(QuotaProfile)
            .where(
                QuotaProfile.role_id
                == sa.select(Role.id).where(Role.code == "guest").scalar_subquery()
            )
            .values(daily_limit=99)
        )
        await session.commit()

        await ensure_roles(session)
        quota = await load_quota(session, RoleCode.GUEST)

    assert quota.daily_limit == 99


async def test_guest_quota_matches_the_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await ensure_roles(session)
        quota = await load_quota(session, RoleCode.GUEST)

    assert (quota.daily_limit, quota.concurrent_limit) == (5, 1)
    assert (quota.bucket_capacity, quota.bucket_refill_minutes) == (1, 30)
    assert not quota.unlimited


async def test_user_quota_matches_the_plan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await ensure_roles(session)
        quota = await load_quota(session, RoleCode.USER)

    assert (quota.daily_limit, quota.concurrent_limit) == (16, 4)
    assert (quota.bucket_capacity, quota.bucket_refill_minutes) == (4, 20)


async def test_superadmin_is_unlimited(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await ensure_roles(session)
        quota = await load_quota(session, RoleCode.SUPERADMIN)

    assert quota.unlimited

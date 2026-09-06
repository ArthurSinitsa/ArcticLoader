"""Срок временного пароля — раздел 2.6 плана.

«Временный пароль живёт 48 часов. Не активировали — заявка сгорает, учётка не
создаётся.» Пароль знает не только владелец: он прошёл через бота и, возможно,
через переписку, поэтому вечно жить не может.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import RegistrationRequest, RequestStatus, User
from app.services import registration
from app.services.roles import ensure_roles
from app.worker.cleanup import cleanup_task
from tests.test_auth import PASSWORD, login
from tests.test_auth import add_user as _add_user

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


async def add_user(session_factory: async_sessionmaker[AsyncSession], **kwargs: Any) -> User:
    """Учётку не завести без справочника ролей — в бою его сеет lifespan."""
    async with session_factory() as session:
        await ensure_roles(session)
    return await _add_user(session_factory, **kwargs)


async def make_temporary_user(
    session_factory: async_sessionmaker[AsyncSession], *, expires_at: datetime | None
) -> User:
    user = await add_user(session_factory, must_change_password=True)
    async with session_factory() as session:
        await session.execute(
            sa.update(User).where(User.id == user.id).values(password_expires_at=expires_at)
        )
        await session.commit()
    return user


async def test_fresh_temporary_password_works(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await make_temporary_user(session_factory, expires_at=datetime.now(UTC) + timedelta(hours=1))

    assert (await login(client)).status_code == 204


async def test_expired_temporary_password_is_refused(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await make_temporary_user(session_factory, expires_at=datetime.now(UTC) - timedelta(hours=1))

    response = await login(client)

    assert response.status_code == 403
    assert "срок" in response.json()["detail"].lower()


async def test_permanent_password_never_expires(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """У сменённого пароля срока нет — иначе людей выкидывало бы раз в двое суток."""
    await add_user(session_factory)

    assert (await login(client)).status_code == 204


async def test_password_change_removes_the_deadline(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user = await make_temporary_user(
        session_factory, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    await login(client)

    await client.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": "новый-длинный-пароль"},
    )

    async with session_factory() as session:
        stored = await session.get(User, user.id)
    assert stored.password_expires_at is None


async def test_cleanup_removes_unactivated_account(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Учётка, которой так и не воспользовались, не должна оставаться навсегда."""
    user = await make_temporary_user(session_factory, expires_at=NOW - timedelta(hours=1))
    ctx: dict[str, Any] = {"settings": settings, "session_factory": session_factory}

    await cleanup_task(ctx, now=NOW)

    async with session_factory() as session:
        assert await session.get(User, user.id) is None


async def test_cleanup_marks_the_request_as_burnt(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """По заявке потом видно, что человек так и не пришёл."""
    async with session_factory() as session:
        request = await registration.create(
            session,
            telegram_id=777001,
            telegram_username=None,
            email="новичок@example.com",
            name="Новичок",
        )
        await registration.mark_approved(session, request.id, processed_by=None)

    user = await add_user(session_factory, email="новичок@example.com", must_change_password=True)
    async with session_factory() as session:
        await session.execute(
            sa.update(User)
            .where(User.id == user.id)
            .values(password_expires_at=NOW - timedelta(hours=1))
        )
        await session.commit()

    ctx: dict[str, Any] = {"settings": settings, "session_factory": session_factory}
    await cleanup_task(ctx, now=NOW)

    async with session_factory() as session:
        stored = await session.get(RegistrationRequest, request.id)
    assert stored.status is RequestStatus.EXPIRED


async def test_cleanup_keeps_activated_accounts(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Сменил пароль — учётка обычная, срок к ней больше не относится."""
    user = await add_user(session_factory)
    ctx: dict[str, Any] = {"settings": settings, "session_factory": session_factory}

    await cleanup_task(ctx, now=NOW)

    async with session_factory() as session:
        assert await session.get(User, user.id) is not None

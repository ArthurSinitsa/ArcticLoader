"""Очередь заявок в админке — раздел 2.6 плана.

Одобрение — единственный путь появления учётки, кроме первого суперадмина из
`.env`. Поэтому здесь же выдача временного пароля, привязка Telegram и записи
в журнал.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditEntry, RegistrationRequest, RequestStatus, User
from app.services import notifications, registration
from app.services.roles import RoleCode
from tests.conftest import FakeArqPool
from tests.test_auth import add_user, login

ADMIN = "admin@example.com"
TELEGRAM_ID = 777001
EMAIL = "новичок@example.com"


async def make_request(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email: str = EMAIL,
    telegram_id: int = TELEGRAM_ID,
) -> RegistrationRequest:
    async with session_factory() as session:
        return await registration.create(
            session,
            telegram_id=telegram_id,
            telegram_username="novichok",
            email=email,
            name="Новичок",
        )


async def sign_in_admin(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    role: RoleCode = RoleCode.ADMIN,
) -> User:
    admin = await add_user(session_factory, email=ADMIN, role=role)
    await login(client, email=ADMIN)
    return admin


async def outbox(redis: FakeArqPool) -> list[str]:
    """Что бот должен будет отправить."""
    items = await redis.lrange(notifications.QUEUE, 0, -1)
    return [item.decode() if isinstance(item, bytes) else item for item in items]


async def test_plain_user_sees_no_queue(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory)
    await login(client)

    assert (await client.get("/api/admin/requests")).status_code == 403


async def test_queue_shows_pending(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await make_request(session_factory)
    await sign_in_admin(client, session_factory)

    body = (await client.get("/api/admin/requests")).json()

    assert body["total"] == 1
    assert body["items"][0]["email"] == EMAIL
    assert body["items"][0]["telegram_username"] == "novichok"


async def test_approval_creates_the_account(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    request = await make_request(session_factory)
    await sign_in_admin(client, session_factory)

    response = await client.post(f"/api/admin/requests/{request.id}/approve")

    assert response.status_code == 200
    async with session_factory() as session:
        user = await session.scalar(sa.select(User).where(User.email == EMAIL))
    assert user.name == "Новичок"
    assert user.must_change_password


async def test_approval_binds_telegram(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Привязка даёт бесплатный канал для сброса пароля и уведомлений."""
    request = await make_request(session_factory)
    await sign_in_admin(client, session_factory)

    await client.post(f"/api/admin/requests/{request.id}/approve")

    async with session_factory() as session:
        user = await session.scalar(sa.select(User).where(User.email == EMAIL))
    assert user.telegram_id == TELEGRAM_ID


async def test_temporary_password_expires(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
) -> None:
    """Раздел 2.6: не активировали за двое суток — доступа нет."""
    request = await make_request(session_factory)
    await sign_in_admin(client, session_factory)

    await client.post(f"/api/admin/requests/{request.id}/approve")

    async with session_factory() as session:
        user = await session.scalar(sa.select(User).where(User.email == EMAIL))
    deadline = user.password_expires_at.replace(tzinfo=user.password_expires_at.tzinfo or UTC)
    expected = datetime.now(UTC) + timedelta(hours=settings.temporary_password_hours)
    assert abs((deadline - expected).total_seconds()) < 60


async def test_password_goes_to_the_bot_not_to_the_admin(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    arq_pool: FakeArqPool,
) -> None:
    """Пароль знает только владелец: админ передаёт доступ, а не пароль."""
    request = await make_request(session_factory)
    await sign_in_admin(client, session_factory)

    response = await client.post(f"/api/admin/requests/{request.id}/approve")

    messages = await outbox(arq_pool)
    assert any(EMAIL in message for message in messages)

    # Пароль ушёл в бота; в ответе админу его быть не должно.
    password = re.search(r"Временный пароль: (\S+)", messages[0]).group(1)
    assert password not in response.text


async def test_approval_closes_the_request(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    request = await make_request(session_factory)
    admin = await sign_in_admin(client, session_factory)

    await client.post(f"/api/admin/requests/{request.id}/approve")

    async with session_factory() as session:
        stored = await session.get(RegistrationRequest, request.id)
    assert stored.status is RequestStatus.APPROVED
    assert stored.processed_by == admin.id


async def test_rejection_tells_the_person(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    arq_pool: FakeArqPool,
) -> None:
    """Молчание в ответ на заявку человек читает как поломку бота."""
    request = await make_request(session_factory)
    await sign_in_admin(client, session_factory)

    response = await client.post(f"/api/admin/requests/{request.id}/reject")

    assert response.status_code == 200
    async with session_factory() as session:
        stored = await session.get(RegistrationRequest, request.id)
    assert stored.status is RequestStatus.REJECTED
    assert await outbox(arq_pool)


async def test_rejection_creates_no_account(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    request = await make_request(session_factory)
    await sign_in_admin(client, session_factory)

    await client.post(f"/api/admin/requests/{request.id}/reject")

    async with session_factory() as session:
        assert await session.scalar(sa.select(User).where(User.email == EMAIL)) is None


async def test_second_decision_is_refused(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Два админа открыли очередь одновременно — второй не должен завести дубль."""
    request = await make_request(session_factory)
    await sign_in_admin(client, session_factory)
    await client.post(f"/api/admin/requests/{request.id}/approve")

    response = await client.post(f"/api/admin/requests/{request.id}/approve")

    assert response.status_code == 409


async def test_taken_email_cannot_be_approved(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Пока заявка ждала, адрес мог занять админ вручную."""
    request = await make_request(session_factory)
    await add_user(session_factory, email=EMAIL)
    await sign_in_admin(client, session_factory)

    response = await client.post(f"/api/admin/requests/{request.id}/approve")

    assert response.status_code == 409


async def test_decision_is_written_to_the_journal(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    request = await make_request(session_factory)
    admin = await sign_in_admin(client, session_factory)

    await client.post(f"/api/admin/requests/{request.id}/approve")

    async with session_factory() as session:
        entry = await session.scalar(
            sa.select(AuditEntry).where(AuditEntry.action == "request.approve")
        )
    assert entry.actor_id == admin.id
    # Пароль в журнал не попадает: журнал читают чаще, чем заводят учётки.
    assert "password" not in str(entry.payload)

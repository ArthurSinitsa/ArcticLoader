"""Уведомления и сброс пароля через бота — Этап 6 плана."""

from typing import Any

import pytest
import sqlalchemy as sa
from fakeredis import FakeAsyncRedis
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import RegistrationRequest, User
from app.services import notifications, registration
from app.services.bot import dialog
from app.services.roles import RoleCode, ensure_roles
from app.services.telegram import Update
from tests.conftest import FakeArqPool
from tests.test_auth import add_user as _add_user
from tests.test_auth import login

CHAT = 777001
ADMIN = "admin@example.com"


@pytest.fixture
async def redis() -> FakeAsyncRedis:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


async def add_user(session_factory: async_sessionmaker[AsyncSession], **kwargs: Any) -> User:
    async with session_factory() as session:
        await ensure_roles(session)
    return await _add_user(session_factory, **kwargs)


async def bind(session_factory: async_sessionmaker[AsyncSession], user: User) -> None:
    async with session_factory() as session:
        await session.execute(sa.update(User).where(User.id == user.id).values(telegram_id=CHAT))
        await session.commit()


async def say(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis, text: str
) -> str:
    async with session_factory() as session:
        return await dialog.handle(
            session,
            redis,
            Update(update_id=1, chat_id=CHAT, user_id=CHAT, username="arctic_user", text=text),
        )


async def outbox(redis: Any) -> list[str]:
    items = await redis.lrange(notifications.QUEUE, 0, -1)
    return [item.decode() if isinstance(item, bytes) else item for item in items]


async def test_reset_sends_a_new_password(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """Потерянный пароль — самая частая причина писать администратору."""
    user = await add_user(session_factory)
    await bind(session_factory, user)

    answer = await say(session_factory, redis, "/reset")

    assert "пароль" in answer.lower()
    async with session_factory() as session:
        stored = await session.get(User, user.id)
    assert stored.must_change_password
    assert stored.password_expires_at is not None


async def test_reset_needs_a_linked_account(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """Иначе любой желающий сбрасывал бы пароли чужим учёткам."""
    answer = await say(session_factory, redis, "/reset")

    assert "/start" in answer


async def test_reset_invalidates_the_old_password(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    user = await add_user(session_factory)
    await bind(session_factory, user)
    async with session_factory() as session:
        before = (await session.get(User, user.id)).password_hash

    await say(session_factory, redis, "/reset")

    async with session_factory() as session:
        after = (await session.get(User, user.id)).password_hash
    assert before != after


async def test_new_request_alerts_the_admin_chat(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    arq_pool: FakeArqPool,
    settings: Any,
) -> None:
    """Заявка, о которой никто не узнал, лежит в очереди сутками."""
    settings.telegram_alert_chat_id = 999
    async with session_factory() as session:
        await registration.create(
            session,
            telegram_id=CHAT,
            telegram_username="novichok",
            email="новичок@example.com",
            name="Новичок",
        )

    # Алерт ставит в очередь тот, кто создаёт заявку, — здесь это бот.
    await notifications.alert_admins(arq_pool, settings, "новая заявка: новичок@example.com")

    assert any("новичок@example.com" in message for message in await outbox(arq_pool))


async def test_alert_without_chat_is_silently_skipped(arq_pool: FakeArqPool, settings: Any) -> None:
    """Чат не задан — это не повод падать: заявку видно в админке."""
    settings.telegram_alert_chat_id = 0

    await notifications.alert_admins(arq_pool, settings, "новая заявка")

    assert await outbox(arq_pool) == []


async def test_ready_download_notifies_the_owner(
    session_factory: async_sessionmaker[AsyncSession], arq_pool: FakeArqPool
) -> None:
    """Ради этого telegram_id и привязывался к учётке."""
    user = await add_user(session_factory)
    await bind(session_factory, user)

    async with session_factory() as session:
        await notifications.notify_owner(session, arq_pool, user_id=user.id, text="Видео готово")

    assert any("Видео готово" in message for message in await outbox(arq_pool))


async def test_guest_download_notifies_nobody(
    session_factory: async_sessionmaker[AsyncSession], arq_pool: FakeArqPool
) -> None:
    async with session_factory() as session:
        await notifications.notify_owner(session, arq_pool, user_id=None, text="Видео готово")

    assert await outbox(arq_pool) == []


async def test_user_without_telegram_notifies_nobody(
    session_factory: async_sessionmaker[AsyncSession], arq_pool: FakeArqPool
) -> None:
    """Учётку мог завести админ вручную — Telegram у неё не привязан."""
    user = await add_user(session_factory)

    async with session_factory() as session:
        await notifications.notify_owner(session, arq_pool, user_id=user.id, text="Видео готово")

    assert await outbox(arq_pool) == []


async def test_admin_sees_the_request_after_bot_dialog(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """Сквозная проверка: разговор с ботом действительно доходит до админки."""
    await say(session_factory, redis, "/start")
    await say(session_factory, redis, "сквозной@example.com")
    await say(session_factory, redis, "Сквозной")

    await add_user(session_factory, email=ADMIN, role=RoleCode.ADMIN)
    await login(client, email=ADMIN)
    body = (await client.get("/api/admin/requests")).json()

    assert body["total"] == 1
    assert body["items"][0]["email"] == "сквозной@example.com"
    async with session_factory() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(RegistrationRequest)) == 1
        )

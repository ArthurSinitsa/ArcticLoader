"""Диалог регистрации в боте — раздел 2.6 плана.

Три шага: `/start` → email → имя → заявка. Состояние шага живёт в Redis, а не
в памяти процесса: перезапуск бота посреди разговора не должен заставлять
человека начинать заново.
"""

from typing import Any

import pytest
import sqlalchemy as sa
from fakeredis import FakeAsyncRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import RegistrationRequest, User
from app.services.bot import dialog
from app.services.roles import ensure_roles
from app.services.telegram import Update
from tests.test_auth import add_user as _add_user

CHAT = 777001


@pytest.fixture
async def redis() -> FakeAsyncRedis:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


async def add_user(session_factory: async_sessionmaker[AsyncSession], **kwargs: Any) -> User:
    """Учётку не завести без справочника ролей — его сеет lifespan, а здесь мы."""
    async with session_factory() as session:
        await ensure_roles(session)
    return await _add_user(session_factory, **kwargs)


def update(text: str, *, chat_id: int = CHAT, username: str | None = "arctic_user") -> Update:
    return Update(update_id=1, chat_id=chat_id, user_id=chat_id, username=username, text=text)


async def handle(
    session_factory: async_sessionmaker[AsyncSession],
    redis: FakeAsyncRedis,
    text: str,
    **kwargs: Any,
) -> str:
    async with session_factory() as session:
        return await dialog.handle(session, redis, update(text, **kwargs))


async def test_start_asks_for_email(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    answer = await handle(session_factory, redis, "/start")

    assert "email" in answer.lower()


async def test_full_path_creates_a_request(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    await handle(session_factory, redis, "/start")
    await handle(session_factory, redis, "новичок@example.com")

    answer = await handle(session_factory, redis, "Новичок")

    async with session_factory() as session:
        request = await session.scalar(sa.select(RegistrationRequest))
    assert request.email == "новичок@example.com"
    assert request.name == "Новичок"
    assert request.telegram_username == "arctic_user"
    assert "заявк" in answer.lower()


async def test_bad_email_does_not_move_the_dialog_on(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """Иначе именем окажется вторая половина неправильного адреса."""
    await handle(session_factory, redis, "/start")

    answer = await handle(session_factory, redis, "это не почта")

    assert "email" in answer.lower()
    async with session_factory() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(RegistrationRequest)) == 0
        )


async def test_message_without_start_explains_what_to_do(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    answer = await handle(session_factory, redis, "привет")

    assert "/start" in answer


async def test_dialog_survives_restart(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """Состояние в Redis: перезапуск бота не должен ронять разговор."""
    await handle(session_factory, redis, "/start")
    await handle(session_factory, redis, "новичок@example.com")

    # Всё, что бот помнит о разговоре, лежит снаружи процесса.
    answer = await handle(session_factory, redis, "Новичок")

    assert "заявк" in answer.lower()


async def test_second_start_begins_anew(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """Передумал на середине — `/start` возвращает к первому вопросу."""
    await handle(session_factory, redis, "/start")
    await handle(session_factory, redis, "первый@example.com")

    answer = await handle(session_factory, redis, "/start")

    assert "email" in answer.lower()


async def test_known_user_is_not_asked_to_register(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """У привязанной учётки регистрация уже позади — ей нужен другой ответ."""
    user = await add_user(session_factory)
    async with session_factory() as session:
        await session.execute(sa.update(User).where(User.id == user.id).values(telegram_id=CHAT))
        await session.commit()

    answer = await handle(session_factory, redis, "/start")

    assert "email" not in answer.lower()
    async with session_factory() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(RegistrationRequest)) == 0
        )


async def test_taken_email_is_refused_early(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """Заявка на занятый адрес всё равно упрётся в дубликат при одобрении."""
    await add_user(session_factory, email="занято@example.com")
    await handle(session_factory, redis, "/start")

    answer = await handle(session_factory, redis, "занято@example.com")

    assert "уже" in answer.lower()
    async with session_factory() as session:
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(RegistrationRequest)) == 0
        )


async def test_name_falls_back_to_telegram_profile(
    session_factory: async_sessionmaker[AsyncSession], redis: FakeAsyncRedis
) -> None:
    """План: имя берётся из профиля, если человек его не указал."""
    await handle(session_factory, redis, "/start")
    await handle(session_factory, redis, "новичок@example.com")

    await handle(session_factory, redis, "-")

    async with session_factory() as session:
        request = await session.scalar(sa.select(RegistrationRequest))
    assert request.name == "arctic_user"

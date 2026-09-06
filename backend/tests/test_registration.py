"""Заявки на регистрацию — раздел 2.6 плана.

Публичной формы signup нет: человек приходит в бота, оставляет email и имя,
админ одобряет вручную. Здесь проверяется всё, что происходит с заявкой до
того, как ею займётся админка.
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import RegistrationRequest, RequestStatus
from app.services import registration

TELEGRAM_ID = 777001
EMAIL = "новичок@example.com"


async def test_request_is_created(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        request = await registration.create(
            session,
            telegram_id=TELEGRAM_ID,
            telegram_username="novichok",
            email=EMAIL,
            name="Новичок",
        )

    assert request.status is RequestStatus.PENDING
    assert request.email == EMAIL
    assert request.telegram_username == "novichok"


async def test_email_is_stored_in_lower_case(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Иначе `Ivan@` и `ivan@` заведут две учётки на одного человека."""
    async with session_factory() as session:
        request = await registration.create(
            session,
            telegram_id=TELEGRAM_ID,
            telegram_username=None,
            email="Ivan@Example.COM",
            name="Иван",
        )

    assert request.email == "ivan@example.com"


async def test_second_request_replaces_the_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Человек ошибся в почте и начал заново — двух заявок от него быть не должно."""
    async with session_factory() as session:
        await registration.create(
            session,
            telegram_id=TELEGRAM_ID,
            telegram_username=None,
            email="опечатка@example.com",
            name="Новичок",
        )

        await registration.create(
            session, telegram_id=TELEGRAM_ID, telegram_username=None, email=EMAIL, name="Новичок"
        )

        pending = await registration.pending(session, limit=10)

    assert len(pending) == 1
    assert pending[0].email == EMAIL


async def test_processed_request_does_not_block_a_new_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отклонённому не запрещено попробовать снова — решает всё равно админ."""
    async with session_factory() as session:
        first = await registration.create(
            session, telegram_id=TELEGRAM_ID, telegram_username=None, email=EMAIL, name="Новичок"
        )
        await registration.reject(session, first.id, processed_by=None)

        await registration.create(
            session, telegram_id=TELEGRAM_ID, telegram_username=None, email=EMAIL, name="Новичок"
        )

        total = await session.scalar(sa.select(sa.func.count()).select_from(RegistrationRequest))

    assert total == 2


async def test_pending_shows_only_unprocessed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        first = await registration.create(
            session, telegram_id=1, telegram_username=None, email="a@example.com", name="А"
        )
        await registration.create(
            session, telegram_id=2, telegram_username=None, email="b@example.com", name="Б"
        )
        await registration.reject(session, first.id, processed_by=None)

        pending = await registration.pending(session, limit=10)

    assert [item.email for item in pending] == ["b@example.com"]


async def test_rejection_is_remembered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        request = await registration.create(
            session, telegram_id=TELEGRAM_ID, telegram_username=None, email=EMAIL, name="Новичок"
        )

        await registration.reject(session, request.id, processed_by=None)

        stored = await session.get(RegistrationRequest, request.id)

    assert stored.status is RequestStatus.REJECTED
    assert stored.processed_at is not None

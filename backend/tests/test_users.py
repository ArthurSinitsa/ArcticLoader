"""Первая учётная запись и хранение паролей.

Публичной регистрации в сервисе нет, а бот появится только на Этапе 6 —
значит суперадмина должно быть чем завести с самого начала, иначе система
поднимается без единого пользователя.
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Role, User
from app.security import hash_password, verify_password
from app.services.roles import RoleCode, ensure_roles
from app.services.users import ensure_superadmin

EMAIL = "root@example.com"
PASSWORD = "первый-пароль"


async def _seed(session: AsyncSession) -> None:
    await ensure_roles(session)


async def test_superadmin_is_created_with_the_top_role(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _seed(session)

        await ensure_superadmin(session, email=EMAIL, password=PASSWORD, name="Артур")

        user = await session.scalar(sa.select(User).where(User.email == EMAIL))
        role = await session.get(Role, user.role_id)

    assert user.name == "Артур"
    assert role.code == RoleCode.SUPERADMIN
    assert user.is_active


async def test_first_password_must_be_changed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пароль из .env знает не только владелец учётки — он лежит в файле."""
    async with session_factory() as session:
        await _seed(session)
        await ensure_superadmin(session, email=EMAIL, password=PASSWORD, name="Артур")

        user = await session.scalar(sa.select(User).where(User.email == EMAIL))

    assert user.must_change_password


async def test_password_is_not_stored_as_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _seed(session)
        await ensure_superadmin(session, email=EMAIL, password=PASSWORD, name="Артур")

        user = await session.scalar(sa.select(User).where(User.email == EMAIL))

    assert PASSWORD not in user.password_hash
    assert verify_password(PASSWORD, user.password_hash)


async def test_second_run_does_not_duplicate_the_account(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _seed(session)
        await ensure_superadmin(session, email=EMAIL, password=PASSWORD, name="Артур")
        await ensure_superadmin(session, email=EMAIL, password=PASSWORD, name="Артур")

        count = await session.scalar(sa.select(sa.func.count()).select_from(User))

    assert count == 1


async def test_restart_does_not_restore_the_env_password(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сменил пароль — перезапуск API не должен возвращать старый из файла."""
    async with session_factory() as session:
        await _seed(session)
        await ensure_superadmin(session, email=EMAIL, password=PASSWORD, name="Артур")
        await session.execute(
            sa.update(User)
            .where(User.email == EMAIL)
            .values(password_hash=hash_password("новый-пароль"), must_change_password=False)
        )
        await session.commit()

        await ensure_superadmin(session, email=EMAIL, password=PASSWORD, name="Артур")

        user = await session.scalar(sa.select(User).where(User.email == EMAIL))

    assert verify_password("новый-пароль", user.password_hash)
    assert not user.must_change_password


async def test_without_settings_no_account_appears(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пустой email в .env — обычная ситуация, а не повод падать на старте."""
    async with session_factory() as session:
        await _seed(session)

        await ensure_superadmin(session, email="", password="", name="")

        count = await session.scalar(sa.select(sa.func.count()).select_from(User))

    assert count == 0


def test_same_password_gives_different_hashes() -> None:
    """Соль внутри хеша: одинаковые пароли не должны быть видны как одинаковые."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_wrong_password_is_rejected() -> None:
    assert not verify_password("не тот", hash_password(PASSWORD))

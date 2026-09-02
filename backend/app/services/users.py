"""Операции над учётными записями."""

import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, User
from app.security import hash_password
from app.services.roles import RoleCode

log = logging.getLogger(__name__)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(sa.select(User).where(User.email == email))


async def ensure_superadmin(
    session: AsyncSession, *, email: str, password: str, name: str
) -> User | None:
    """Завести первую учётку из настроек, если её ещё нет.

    Публичной регистрации нет, а бот с заявками появится только на Этапе 6 —
    без этого шага систему некому администрировать. Существующую запись не
    трогаем: пароль из `.env` знает не только владелец, и возвращать его при
    каждом перезапуске означало бы отменять любую смену.
    """
    if not email or not password:
        log.info("суперадмин в настройках не задан — учётка не создаётся")
        return None

    if await get_by_email(session, email) is not None:
        return None

    role_id = await session.scalar(sa.select(Role.id).where(Role.code == RoleCode.SUPERADMIN))
    if role_id is None:
        raise LookupError("справочник ролей пуст: сначала ensure_roles")

    user = User(
        email=email,
        name=name or email,
        password_hash=hash_password(password),
        role_id=role_id,
        # Пароль лежит в файле на диске — до первой смены доступ считается
        # временным (раздел 2.6).
        must_change_password=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    log.warning("создан суперадмин %s — смените пароль при первом входе", email)
    return user

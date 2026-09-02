"""Зависимости FastAPI: всё берётся из состояния приложения, глобалов нет."""

import hashlib
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Annotated

from arq.connections import ArqRedis
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.quotas import Quota
from app.services.roles import RoleCode, load_quota


async def get_session(request: Request) -> AsyncGenerator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_arq_pool(request: Request) -> ArqRedis:
    return request.app.state.arq_pool


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ArqDep = Annotated[ArqRedis, Depends(get_arq_pool)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


@dataclass(frozen=True)
class Principal:
    """Кто именно пришёл за загрузкой.

    Пока это всегда гость: вход и сессии — следующий шаг Этапа 4. Всё
    остальное уже считается по субъекту, поэтому появление пользователей
    сведётся к замене начинки `get_principal`.
    """

    role: RoleCode
    quota: Quota
    user_id: uuid.UUID | None = None
    fingerprint: str | None = None

    @property
    def is_guest(self) -> bool:
        return self.user_id is None

    @property
    def subject(self) -> str:
        """Ключ квоты: у авторизованных — учётка, у гостей — отпечаток."""
        return f"guest:{self.fingerprint}" if self.is_guest else f"user:{self.user_id}"


def guest_fingerprint(request: Request) -> str:
    """Отпечаток гостя — раздел 4.4.

    Связка IP и User-Agent, а не один IP: за общим офисным NAT все посетители
    иначе делили бы одну квоту на всех. Полноценный браузерный отпечаток
    придёт вместе с Turnstile.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    # Первый адрес в цепочке — клиент; остальное дописали прокси.
    ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else "")
    agent = request.headers.get("user-agent", "")
    return hashlib.sha256(f"{ip}|{agent}".encode()).hexdigest()


async def get_principal(request: Request, session: SessionDep) -> Principal:
    quota = await load_quota(session, RoleCode.GUEST)
    return Principal(role=RoleCode.GUEST, quota=quota, fingerprint=guest_fingerprint(request))


PrincipalDep = Annotated[Principal, Depends(get_principal)]

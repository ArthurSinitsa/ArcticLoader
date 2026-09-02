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
from app.models import User
from app.services import sessions
from app.services.quotas import Quota
from app.services.roles import RoleCode, load_quota, load_quota_by_role_id


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
    """Кто именно пришёл за загрузкой: пользователь сессии или гость."""

    role: RoleCode
    quota: Quota
    user: User | None = None
    fingerprint: str | None = None

    @property
    def is_guest(self) -> bool:
        return self.user is None

    @property
    def user_id(self) -> uuid.UUID | None:
        return self.user.id if self.user else None

    @property
    def must_change_password(self) -> bool:
        return bool(self.user and self.user.must_change_password)

    @property
    def subject(self) -> str:
        """Ключ квоты: у авторизованных — учётка, у гостей — отпечаток."""
        return f"guest:{self.fingerprint}" if self.is_guest else f"user:{self.user_id}"


def client_ip(request: Request) -> str:
    """Адрес клиента с поправкой на прокси: сервис всегда стоит за Caddy."""
    forwarded = request.headers.get("x-forwarded-for", "")
    # Первый адрес в цепочке — клиент; остальное дописали прокси.
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "")


def guest_fingerprint(request: Request) -> str:
    """Отпечаток гостя — раздел 4.4.

    Связка IP и User-Agent, а не один IP: за общим офисным NAT все посетители
    иначе делили бы одну квоту на всех. Полноценный браузерный отпечаток
    придёт вместе с Turnstile.
    """
    agent = request.headers.get("user-agent", "")
    return hashlib.sha256(f"{client_ip(request)}|{agent}".encode()).hexdigest()


async def get_principal(request: Request, session: SessionDep) -> Principal:
    """Субъект запроса. Нет живой сессии — значит гость."""
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)

    if token:
        user = await _user_by_session(request, session, token, settings)
        if user is not None:
            code, quota = await load_quota_by_role_id(session, user.role_id)
            return Principal(role=code, quota=quota, user=user)

    quota = await load_quota(session, RoleCode.GUEST)
    return Principal(role=RoleCode.GUEST, quota=quota, fingerprint=guest_fingerprint(request))


async def _user_by_session(
    request: Request, session: AsyncSession, token: str, settings: Settings
) -> User | None:
    user_id = await sessions.resolve(
        request.app.state.arq_pool, token, ttl=settings.session_ttl_seconds
    )
    if user_id is None:
        return None

    user = await session.get(User, user_id)
    # Учётку могли выключить, пока сессия жила: доступ пропадает сразу.
    return user if user is not None and user.is_active else None


PrincipalDep = Annotated[Principal, Depends(get_principal)]

"""Раздел «Администрирование» — Этап 5 плана.

Права разложены по матрице 2.5.1. Ключевое её правило: админ не повышает роли
и не трогает других админов — иначе любой админ становится суперадмином за два
клика, и разделение уровней перестаёт что-либо значить.

Каждое действие, меняющее чужие данные, оставляет запись в журнале (3.6).
"""

import logging
import secrets
import uuid
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, status

from app.api.deps import AdminDep, Principal, SessionDep, SuperadminDep, client_ip
from app.models import TERMINAL_STATUSES, QuotaProfile, Role, Task, User
from app.schemas import (
    AdminTaskOut,
    AdminTaskPage,
    AuditOut,
    AuditPage,
    FlagUpdate,
    QuotaOut,
    QuotaUpdate,
    UserCreate,
    UserCreated,
    UserOut,
    UserPage,
    UserUpdate,
)
from app.security import hash_password
from app.services import audit, flags
from app.services.roles import RoleCode

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

#: Роли, которые админу не подчиняются: разбирать конфликты между админами
#: может только суперадмин.
PROTECTED_ROLES = frozenset({RoleCode.ADMIN, RoleCode.SUPERADMIN})

#: Временный пароль: достаточно длинный, чтобы не подбирался за время своей
#: жизни, и достаточно короткий, чтобы его продиктовали голосом.
TEMPORARY_PASSWORD_BYTES = 9

SORTABLE = {
    "created_at": User.created_at,
    "email": User.email,
    "name": User.name,
    "last_login_at": User.last_login_at,
}


@router.get("/users")
async def list_users(
    admin: AdminDep,
    session: SessionDep,
    search: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    sort: str = "created_at",
    order: str = "desc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserPage:
    query = sa.select(User, Role.code).join(Role, Role.id == User.role_id)
    counter = sa.select(sa.func.count()).select_from(User).join(Role, Role.id == User.role_id)

    if search:
        # Ищут по памяти, а не по точному написанию — отсюда регистронезависимость.
        pattern = f"%{search.strip().lower()}%"
        condition = sa.or_(
            sa.func.lower(User.email).like(pattern), sa.func.lower(User.name).like(pattern)
        )
        query, counter = query.where(condition), counter.where(condition)

    if role:
        query, counter = query.where(Role.code == role), counter.where(Role.code == role)

    if is_active is not None:
        query = query.where(User.is_active.is_(is_active))
        counter = counter.where(User.is_active.is_(is_active))

    column = SORTABLE.get(sort, User.created_at)
    query = query.order_by(column.asc() if order == "asc" else column.desc())

    rows = (await session.execute(query.limit(limit).offset(offset))).all()
    total = await session.scalar(counter)

    return UserPage(items=[_to_out(user, code) for user, code in rows], total=total)


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate, request: Request, admin: AdminDep, session: SessionDep
) -> UserCreated:
    _ensure_may_touch_role(admin, payload.role)

    if await session.scalar(sa.select(User.id).where(User.email == payload.email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "такой email уже занят")

    role_id = await _role_id(session, payload.role)
    password = secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)

    user = User(
        email=payload.email,
        name=payload.name,
        password_hash=hash_password(password),
        role_id=role_id,
        # Пароль сообщат человеку голосом или в мессенджере — до смены он
        # считается временным (раздел 2.6).
        must_change_password=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    await audit.record(
        session,
        actor_id=admin.user_id,
        action="user.create",
        target_type="user",
        target_id=str(user.id),
        # Пароля здесь нет намеренно: журнал читают чаще, чем заводят учётки.
        payload={"email": user.email, "role": payload.role},
        ip=client_ip(request),
    )

    return UserCreated(user=_to_out(user, payload.role), temporary_password=password)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    request: Request,
    admin: AdminDep,
    session: SessionDep,
) -> UserOut:
    user, code = await _load_user(session, user_id)
    _ensure_may_touch_role(admin, code)

    changes: dict[str, Any] = {}
    if payload.name is not None:
        changes["name"] = payload.name
    if payload.is_active is not None:
        changes["is_active"] = payload.is_active

    if payload.role is not None and payload.role != code:
        # Повышение роли — только суперадмин: иначе админ повысит сам себя.
        if admin.role is not RoleCode.SUPERADMIN:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "роли меняет только суперадмин")
        _ensure_may_touch_role(admin, payload.role)
        changes["role_id"] = await _role_id(session, payload.role)
        code = payload.role

    if changes:
        await session.execute(sa.update(User).where(User.id == user_id).values(**changes))
        await session.commit()
        await session.refresh(user)
        await audit.record(
            session,
            actor_id=admin.user_id,
            action="user.update",
            target_type="user",
            target_id=str(user_id),
            payload={key: str(value) for key, value in changes.items()},
            ip=client_ip(request),
        )

    return _to_out(user, code)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID, request: Request, admin: AdminDep, session: SessionDep
) -> None:
    if user_id == admin.user_id:
        # Самый простой способ остаться без единого администратора.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "нельзя удалить самого себя")

    user, code = await _load_user(session, user_id)
    _ensure_may_touch_role(admin, code)

    await session.delete(user)
    await session.commit()

    await audit.record(
        session,
        actor_id=admin.user_id,
        action="user.delete",
        target_type="user",
        target_id=str(user_id),
        payload={"email": user.email, "role": code},
        ip=client_ip(request),
    )


@router.get("/quotas")
async def list_quotas(admin: AdminDep, session: SessionDep) -> list[QuotaOut]:
    rows = (
        await session.execute(
            sa.select(Role, QuotaProfile)
            .join(QuotaProfile, QuotaProfile.role_id == Role.id)
            .order_by(Role.id)
        )
    ).all()
    return [
        QuotaOut(
            role=role.code,
            title=role.title,
            is_unlimited=role.is_unlimited,
            daily_limit=profile.daily_limit,
            concurrent_limit=profile.concurrent_limit,
            bucket_capacity=profile.bucket_capacity,
            bucket_refill_minutes=profile.bucket_refill_minutes,
        )
        for role, profile in rows
    ]


@router.put("/quotas/{role_code}")
async def update_quota(
    role_code: str,
    payload: QuotaUpdate,
    request: Request,
    admin: SuperadminDep,
    session: SessionDep,
) -> QuotaOut:
    """Правка лимитов — раздел 6: цифры живут в БД, а не в коде."""
    role = await session.scalar(sa.select(Role).where(Role.code == role_code))
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "нет такой роли")

    await session.execute(
        sa.update(QuotaProfile)
        .where(QuotaProfile.role_id == role.id)
        .values(**payload.model_dump())
    )
    await session.commit()

    await audit.record(
        session,
        actor_id=admin.user_id,
        action="quota.update",
        target_type="role",
        target_id=role_code,
        payload=payload.model_dump(),
        ip=client_ip(request),
    )

    profile = await session.get(QuotaProfile, role.id)
    return QuotaOut(
        role=role.code,
        title=role.title,
        is_unlimited=role.is_unlimited,
        daily_limit=profile.daily_limit,
        concurrent_limit=profile.concurrent_limit,
        bucket_capacity=profile.bucket_capacity,
        bucket_refill_minutes=profile.bucket_refill_minutes,
    )


@router.get("/audit")
async def read_audit(
    admin: SuperadminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditPage:
    entries = await audit.recent(session, limit=limit, offset=offset)
    return AuditPage(items=[AuditOut.model_validate(entry) for entry in entries])


def _to_out(user: User, role_code: str) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=role_code,
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


async def _load_user(session: SessionDep, user_id: uuid.UUID) -> tuple[User, str]:
    row = (
        await session.execute(
            sa.select(User, Role.code).join(Role, Role.id == User.role_id).where(User.id == user_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "учётная запись не найдена")
    return row[0], row[1]


def _ensure_may_touch_role(admin: Principal, role_code: str) -> None:
    """Админу подчиняются только обычные пользователи.

    И заводить, и править, и удалять админов может лишь суперадмин: иначе
    админ создаёт себе второго или снимает чужие ограничения.
    """
    if admin.role is RoleCode.SUPERADMIN:
        return
    if role_code in PROTECTED_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "учётными записями администраторов ведает суперадмин"
        )


async def _role_id(session: SessionDep, role_code: str) -> int:
    role_id = await session.scalar(sa.select(Role.id).where(Role.code == role_code))
    if role_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "нет такой роли")
    return role_id


@router.get("/tasks")
async def list_tasks(
    admin: AdminDep,
    session: SessionDep,
    active_only: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminTaskPage:
    """Что происходит на сервере прямо сейчас.

    По умолчанию только незавершённые: экран про текущий момент, а не про
    историю — иначе прерывать в нём нечего.
    """
    query = sa.select(Task, User.email).outerjoin(User, User.id == Task.user_id)
    counter = sa.select(sa.func.count()).select_from(Task)

    if active_only:
        query = query.where(Task.status.not_in(TERMINAL_STATUSES))
        counter = counter.where(Task.status.not_in(TERMINAL_STATUSES))

    rows = (
        await session.execute(query.order_by(Task.created_at.desc()).limit(limit).offset(offset))
    ).all()

    return AdminTaskPage(
        items=[
            AdminTaskOut(
                id=task.id,
                owner=email,
                source_url=task.source_url,
                title=task.title,
                quality=task.quality,
                status=task.status,
                progress=task.progress,
                worker_pid=task.worker_pid,
                created_at=task.created_at,
            )
            for task, email in rows
        ],
        total=await session.scalar(counter),
    )


@router.get("/flags")
async def read_flags(admin: AdminDep, session: SessionDep) -> dict[str, bool]:
    """Состояние рубильников. Видит и админ — чтобы понимать, почему сервис
    ведёт себя не так, как обычно."""
    return await flags.current(session)


@router.put("/flags/{key}")
async def switch_flag(
    key: str,
    payload: FlagUpdate,
    request: Request,
    admin: SuperadminDep,
    session: SessionDep,
) -> dict[str, bool]:
    try:
        await flags.set_flag(session, key, payload.enabled)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "нет такого рубильника") from exc

    await audit.record(
        session,
        actor_id=admin.user_id,
        action="flag.set",
        target_type="flag",
        target_id=key,
        payload={"enabled": payload.enabled},
        ip=client_ip(request),
    )
    return await flags.current(session)

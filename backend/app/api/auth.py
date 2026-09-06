"""Вход, выход и смена пароля — разделы 2.6 и 4.4 плана.

Публичной регистрации нет: учётки заводит админ или бот по заявке. Поэтому
здесь только вход, и он же — самая уязвимая точка публичного адреса, отсюда
защита от перебора.
"""

import logging
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.deps import ArqDep, PrincipalDep, SessionDep, SettingsDep, client_ip
from app.config import Settings
from app.models import User
from app.schemas import CurrentUser, LoginRequest, PasswordChange
from app.security import hash_password, verify_password
from app.services import login_guard, sessions
from app.services import users as users_repo

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: Ответ на любую неудачу входа один и тот же: разные тексты для «нет такого
#: адреса» и «пароль не тот» превращают форму входа в список учётных записей.
INVALID_CREDENTIALS = "неверный email или пароль"

#: Хеш заведомо недостижимого пароля. Сверяемся с ним, когда учётки нет, чтобы
#: несуществующий адрес отвечал столько же времени, сколько существующий.
_DUMMY_HASH = hash_password("отсутствующая-учётная-запись")


@router.post("/login", status_code=status.HTTP_204_NO_CONTENT)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    arq: ArqDep,
    settings: SettingsDep,
) -> None:
    ip = client_ip(request)
    blocked = await login_guard.blocked_for(
        arq, ip=ip, login=payload.email, max_attempts=settings.login_max_attempts
    )
    if blocked:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "reason": "login_blocked",
                "retry_after": blocked,
                # Округление вверх: 900 секунд — это 15 минут, а не 16.
                "message": f"Слишком много попыток. Вход откроется через {-(-blocked // 60)} мин.",
            },
            headers={"Retry-After": str(blocked)},
        )

    user = await users_repo.get_by_email(session, payload.email)
    password_ok = verify_password(payload.password, user.password_hash if user else _DUMMY_HASH)

    if user is None or not password_ok:
        failures = await login_guard.register_failure(
            arq,
            ip=ip,
            login=payload.email,
            max_attempts=settings.login_max_attempts,
            block_seconds=settings.login_block_seconds,
        )
        log.warning("неудачный вход %s с %s, попытка %s", payload.email, ip, failures)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_CREDENTIALS)

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "учётная запись отключена")

    if _password_expired(user):
        # Пароль прошёл через бота и, возможно, через переписку — вечно жить
        # он не может (раздел 2.6). Учётку заберёт чистка.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "срок действия временного пароля истёк — обратитесь к администратору",
        )

    await login_guard.reset(arq, ip=ip, login=payload.email)
    await _open_session(response, session, arq, settings, user)
    log.info("вход выполнен: %s", user.email)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, arq: ArqDep, settings: SettingsDep) -> None:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await sessions.destroy(arq, token)
    response.delete_cookie(settings.session_cookie_name, path="/")


@router.get("/me")
async def me(principal: PrincipalDep) -> CurrentUser:
    if principal.user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "нужен вход")
    # Роль лежит не на пользователе, а в справочнике — собираем ответ явно.
    return CurrentUser(
        id=principal.user.id,
        email=principal.user.email,
        name=principal.user.name,
        must_change_password=principal.user.must_change_password,
        role=principal.role.value,
    )


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChange,
    response: Response,
    principal: PrincipalDep,
    session: SessionDep,
    arq: ArqDep,
    settings: SettingsDep,
) -> None:
    """Смена пароля — единственное, что доступно с временным паролем."""
    user = principal.user
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "нужен вход")

    # Добровольная смена требует старый пароль: без этой проверки уведённая
    # cookie превращается в захват учётки — вор сменит пароль и выкинет
    # владельца. При принудительной не спрашиваем: человек минуту назад вошёл
    # этим самым паролем, и второй ввод — просто лишнее трение.
    if not user.must_change_password and not (
        payload.current_password and verify_password(payload.current_password, user.password_hash)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "текущий пароль неверен")

    await session.execute(
        sa.update(User)
        .where(User.id == user.id)
        .values(
            password_hash=hash_password(payload.new_password),
            must_change_password=False,
            # Пароль стал постоянным: срока у него больше нет, иначе людей
            # выкидывало бы раз в двое суток.
            password_expires_at=None,
        )
    )
    await session.commit()

    # Смена пароля обрывает все сессии: иначе укравший cookie остаётся внутри,
    # и смена ничего не даёт. Текущему устройству сразу выдаём новую.
    await sessions.destroy_all(arq, user.id)
    await _open_session(response, session, arq, settings, user, touch_login=False)
    log.info("пароль изменён: %s", user.email)


async def _open_session(
    response: Response,
    session: SessionDep,
    arq: ArqDep,
    settings: Settings,
    user: User,
    *,
    touch_login: bool = True,
) -> None:
    token = await sessions.create(arq, user.id, ttl=settings.session_ttl_seconds)
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_seconds,
        # httponly закрывает cookie от скриптов, lax — от переходов с чужих
        # сайтов; secure включается на проде, где сервис живёт за TLS.
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )

    if touch_login:
        await session.execute(
            sa.update(User).where(User.id == user.id).values(last_login_at=datetime.now(UTC))
        )
        await session.commit()


def _password_expired(user: User) -> bool:
    """Истёк ли временный пароль. У постоянного срока нет вовсе."""
    if user.password_expires_at is None:
        return False
    # SQLite в тестах хранит время без зоны, Postgres — с ней.
    deadline = user.password_expires_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline <= datetime.now(UTC)

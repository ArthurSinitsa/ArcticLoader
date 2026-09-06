"""Разговор с ботом — раздел 2.6 плана.

Три шага: `/start` → email → имя → заявка. Своего фреймворка на такой диалог
не нужно, но состояние шага живёт в Redis, а не в памяти процесса: перезапуск
бота посреди разговора не должен заставлять человека начинать заново.

Функция `handle` ничего не отправляет сама — она возвращает текст ответа.
Так диалог проверяется тестами без сети, а отправкой занимается один процесс,
который и следит за лимитами Telegram.
"""

import logging
import re
import secrets
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.security import hash_password
from app.services import registration
from app.services.telegram import Update

log = logging.getLogger(__name__)

DIALOG_PREFIX = "tg:dialog"

#: Полчаса на разговор из трёх реплик. Дальше человек, скорее всего, ушёл,
#: и хранить его недописанную заявку незачем.
DIALOG_TTL_SECONDS = 1800

STEP_EMAIL = "email"
STEP_NAME = "name"

#: Не полноценная валидация адреса, а отсев явного мусора: настоящую проверку
#: делает админ глазами, когда решает по заявке.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Человек может не захотеть называться — тогда берём имя из профиля Telegram.
_SKIP = {"-", "—", "нет", "пропустить"}

GREETING = (
    "Это Arctic Loader — сервис скачивания видео для своих.\n\n"
    "Публичной регистрации нет: заявку рассматривает администратор.\n"
    "Напишите ваш email — он нужен для входа и восстановления доступа."
)
ASK_NAME = "Теперь имя — как к вам обращаться. Или «-», чтобы взять его из вашего профиля."
BAD_EMAIL = "Это не похоже на email. Напишите адрес целиком, например ivan@example.com"
EMAIL_TAKEN = "На этот адрес уже есть учётная запись. Попробуйте войти или напишите администратору."
DONE = "Заявка отправлена. Администратор рассмотрит её, и бот пришлёт логин с временным паролем."
ALREADY_REGISTERED = "Вы уже зарегистрированы. Бот пришлёт сюда уведомления о готовых загрузках."
HOW_TO_START = "Чтобы оставить заявку на доступ, отправьте /start"

#: Тот же размер, что у паролей из админки: диктуется голосом, но не
#: подбирается за время своей жизни.
TEMPORARY_PASSWORD_BYTES = 9


async def handle(
    session: AsyncSession,
    redis: Redis,
    update: Update,
    *,
    temporary_password_hours: int = 48,
) -> str:
    """Ответ на одно сообщение. Отправку делает вызывающий."""
    text = update.text.strip()

    if text.startswith("/reset"):
        return await _reset_password(session, update, temporary_password_hours)

    if text.startswith("/start"):
        if await _is_registered(session, update.user_id):
            return ALREADY_REGISTERED
        # Начинаем заново: человек мог передумать на середине.
        await _set_step(redis, update.chat_id, STEP_EMAIL)
        return GREETING

    step = await _get_step(redis, update.chat_id)

    if step == STEP_EMAIL:
        return await _take_email(session, redis, update, text)
    if step == STEP_NAME:
        return await _take_name(session, redis, update, text)
    return HOW_TO_START


async def _take_email(session: AsyncSession, redis: Redis, update: Update, text: str) -> str:
    if not _EMAIL.match(text):
        # Шаг не двигаем: иначе именем окажется вторая половина адреса.
        return BAD_EMAIL

    email = text.lower()
    if await session.scalar(sa.select(User.id).where(User.email == email)):
        # Заявка на занятый адрес всё равно упрётся в дубликат при одобрении —
        # лучше сказать об этом сразу, чем спустя сутки ожидания.
        return EMAIL_TAKEN

    await _set_step(redis, update.chat_id, STEP_NAME, email=email)
    return ASK_NAME


async def _take_name(session: AsyncSession, redis: Redis, update: Update, text: str) -> str:
    email = await _get_email(redis, update.chat_id)
    if not email:
        # Разговор протух, пока человек думал над именем.
        return HOW_TO_START

    name = text.strip()
    if name.lower() in _SKIP or not name:
        name = update.username or email.split("@")[0]

    await registration.create(
        session,
        telegram_id=update.user_id,
        telegram_username=update.username,
        email=email,
        name=name,
    )
    await _clear(redis, update.chat_id)
    return DONE


async def _is_registered(session: AsyncSession, telegram_id: int) -> bool:
    return bool(await session.scalar(sa.select(User.id).where(User.telegram_id == telegram_id)))


def _key(chat_id: int) -> str:
    return f"{DIALOG_PREFIX}:{chat_id}"


async def _set_step(redis: Redis, chat_id: int, step: str, *, email: str | None = None) -> None:
    state = {"step": step}
    if email:
        state["email"] = email
    await redis.hset(_key(chat_id), mapping=state)  # type: ignore[arg-type]
    await redis.expire(_key(chat_id), DIALOG_TTL_SECONDS)


async def _get_step(redis: Redis, chat_id: int) -> str | None:
    return _text(await redis.hget(_key(chat_id), "step"))  # type: ignore[misc]


async def _get_email(redis: Redis, chat_id: int) -> str | None:
    return _text(await redis.hget(_key(chat_id), "email"))  # type: ignore[misc]


async def _clear(redis: Redis, chat_id: int) -> None:
    await redis.delete(_key(chat_id))


def _text(raw: bytes | str | None) -> str | None:
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else raw


async def _reset_password(
    session: AsyncSession, update: Update, temporary_password_hours: int
) -> str:
    """Выдать новый временный пароль — раздел 2.6.

    Работает только для привязанной учётки: `telegram_id` подтверждает, что
    пишет её владелец. Иначе любой желающий сбрасывал бы чужие пароли, зная
    один только email.
    """
    user = await session.scalar(
        sa.select(User).where(User.telegram_id == update.user_id, User.is_active.is_(True))
    )
    if user is None:
        return HOW_TO_START

    password = secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)
    user.password_hash = hash_password(password)
    # Пароль снова временный: он ушёл в переписку, и жить вечно не должен.
    user.must_change_password = True
    user.password_expires_at = datetime.now(UTC) + timedelta(hours=temporary_password_hours)
    await session.commit()

    log.info("сброс пароля по запросу из бота: %s", user.email)
    return (
        "Пароль сброшен.\n\n"
        f"Логин: {user.email}\n"
        f"Временный пароль: {password}\n\n"
        f"Он действует {temporary_password_hours} часов — войдите и смените его."
    )

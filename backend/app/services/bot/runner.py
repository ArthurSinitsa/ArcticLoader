"""Процесс бота: цикл long polling — Этап 6 плана.

Отдельный процесс, а не корутина внутри воркера: опрос Telegram — бесконечный
цикл с двадцатипятисекундными паузами, и ему нечего делать рядом с четырьмя
параллельными загрузками.

Здесь же единственная точка отправки сообщений: уведомления от API и воркера
приходят через Redis и уходят в Telegram отсюда. Иначе три процесса
независимо колотились бы в Bot API и вместе упирались в его лимиты.
"""

import asyncio
import contextlib
import logging
import signal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.logging_setup import configure_logging
from app.services import notifications
from app.services.bot import dialog
from app.services.telegram import TelegramClient

log = logging.getLogger(__name__)

#: Пауза после сбоя, чтобы не молотить по недоступному Telegram без остановки.
RETRY_PAUSE_SECONDS = 5.0


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    stop = asyncio.Event()
    _catch_signals(stop)

    if not settings.telegram_bot_token:
        # Не выходим: контейнер с `restart: unless-stopped` тут же поднял бы
        # процесс заново, и логи превратились бы в ленту рестартов. Ждём —
        # токен появится вместе с пересозданием контейнера.
        log.warning("токен бота не задан, работать не с чем — жду")
        await stop.wait()
        return

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url)
    bot = TelegramClient(settings.telegram_bot_token)

    log.info("бот запущен")
    try:
        await asyncio.gather(
            _poll_updates(bot, session_factory, redis, stop, settings),
            _forward_notifications(bot, redis, stop),
        )
    finally:
        await bot.aclose()
        await redis.aclose()
        await engine.dispose()
        log.info("бот остановлен")


async def _poll_updates(
    bot: TelegramClient,
    session_factory: async_sessionmaker[Any],
    redis: Redis,
    stop: asyncio.Event,
    settings: Settings,
) -> None:
    """Опрос Telegram и ответы на сообщения."""
    offset = 0
    while not stop.is_set():
        updates = await bot.get_updates(offset=offset)
        if not updates:
            # Пустой ответ — это либо тишина, либо сбой связи; в обоих случаях
            # просто идём на следующий круг.
            continue

        for update in updates:
            # Сдвиг обновляем до обработки: упавшее сообщение не должно
            # повторяться бесконечно, унося с собой весь диалог.
            offset = update.update_id + 1
            await _answer(bot, session_factory, redis, update, settings)


async def _answer(
    bot: TelegramClient,
    session_factory: async_sessionmaker[Any],
    redis: Redis,
    update: Any,
    settings: Settings,
) -> None:
    try:
        async with session_factory() as session:
            reply = await dialog.handle(
                session,
                redis,
                update,
                temporary_password_hours=settings.temporary_password_hours,
            )
            if reply == dialog.DONE:
                # Заявка, о которой никто не узнал, лежит в очереди сутками.
                await notifications.alert_admins(
                    redis,
                    settings,
                    f"Новая заявка на доступ от @{update.username or update.user_id}",
                )
    except Exception:
        log.exception("не смог обработать сообщение из чата %s", update.chat_id)
        return

    await bot.send_message(update.chat_id, reply)


async def _forward_notifications(bot: TelegramClient, redis: Redis, stop: asyncio.Event) -> None:
    """Сообщения, которые попросили отправить API и воркер."""
    async for message in notifications.listen(redis):
        if stop.is_set():
            return

        # Свой лог вместо httpx: тот печатал URL целиком вместе с токеном.
        # Здесь только адресат — по нему видно, что уведомление ушло.
        if await bot.send_message(message.chat_id, message.text):
            log.info("уведомление отправлено в чат %s", message.chat_id)
        else:
            log.warning("уведомление в чат %s не доставлено", message.chat_id)


def _catch_signals(stop: asyncio.Event) -> None:
    """`docker compose stop` шлёт SIGTERM — выходим по-человечески."""
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        # NotImplementedError — запуск под Windows без Docker: там таких
        # обработчиков нет, и это не мешает работе.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(name, stop.set)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

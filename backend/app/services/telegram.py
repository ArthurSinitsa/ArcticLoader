"""Клиент Telegram Bot API — Этап 6 плана.

Свой тонкий клиент, а не готовый фреймворк: из всего API нужны два метода, а
диалог регистрации состоит из трёх шагов. FSM, роутерам и middleware аиограма
здесь просто нечего обслуживать, зато `httpx` в проекте уже есть.

**Long polling, а не webhook.** Сервис живёт за домашним NAT, публичного
адреса у него нет и Telegram не сможет к нему постучаться. Когда появится
домен, переезд на webhook — правка одного этого модуля.

Ни один метод не бросает исключений наружу: обрыв связи при долгом опросе —
норма, а заблокировавший бота пользователь — обычное дело, и цикл опроса не
должен падать ни от того, ни от другого.
"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

#: Сколько Telegram держит соединение, если сообщений нет. Двадцать пять
#: секунд — компромисс: реже дёргаем сеть, но и перезапуск бота не ждём минуту.
POLL_TIMEOUT_SECONDS = 25

#: Запас поверх времени опроса: ответ обязан прийти позже, чем Telegram
#: отпустит соединение, иначе клиент рвёт его сам и получает вечные таймауты.
REQUEST_TIMEOUT_SECONDS = POLL_TIMEOUT_SECONDS + 10


@dataclass(frozen=True)
class Update:
    """Входящее текстовое сообщение — единственное, что нам интересно."""

    update_id: int
    chat_id: int
    user_id: int
    username: str | None
    text: str


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = API_BASE,
    ) -> None:
        # Токен уходит в путь запроса, а не в тело: тела чаще попадают в логи.
        self._url = f"{base_url}/bot{token}"
        self._owned = client is None
        self._http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)

    async def send_message(self, chat_id: int, text: str) -> bool:
        """Отправить сообщение. False — не доставлено, и это не повод падать."""
        answer = await self._call("sendMessage", {"chat_id": chat_id, "text": text})
        return answer is not None

    async def get_updates(
        self, *, offset: int, timeout: int = POLL_TIMEOUT_SECONDS
    ) -> list[Update]:
        """Забрать новые сообщения.

        `offset` обязателен: без него Telegram отдаёт уже обработанные апдейты
        снова и снова, и бот отвечает на одно сообщение бесконечно.
        """
        answer = await self._call("getUpdates", {"offset": offset, "timeout": timeout})
        if answer is None:
            return []
        return [parsed for raw in answer if (parsed := _parse_update(raw)) is not None]

    async def aclose(self) -> None:
        if self._owned:
            await self._http.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> Any | None:
        try:
            response = await self._http.post(f"{self._url}/{method}", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("телеграм не ответил на %s: %s", method, exc)
            return None

        if not body.get("ok"):
            log.warning("телеграм отклонил %s: %s", method, body.get("description"))
            return None
        return body.get("result")


def _parse_update(raw: dict[str, Any]) -> Update | None:
    """Только новые текстовые сообщения.

    Правки, стикеры и служебные события боту не адресованы, а `edited_message`
    ещё и заставил бы переигрывать уже пройденный шаг диалога.
    """
    message = raw.get("message")
    if not isinstance(message, dict):
        return None

    text = message.get("text")
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if not text or "id" not in chat:
        return None

    return Update(
        update_id=raw["update_id"],
        chat_id=chat["id"],
        user_id=sender.get("id", chat["id"]),
        username=sender.get("username"),
        text=text,
    )

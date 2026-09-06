"""Клиент Telegram Bot API — Этап 6.

Свой тонкий клиент вместо библиотеки: из всего API нужны два метода, а диалог
состоит из трёх шагов. FSM, роутеры и middleware готовых фреймворков здесь
нечего обслуживать.

Сеть не дёргаем: запросы идут через подставной транспорт, зато проверяется
ровно то, что уходит в Telegram.
"""

import json
from typing import Any

import httpx

from app.services.telegram import TelegramClient, Update

TOKEN = "123456:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def client(handler: Any) -> TelegramClient:
    return TelegramClient(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def message(update_id: int, text: str, *, chat_id: int = 42) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "from": {"id": chat_id, "username": "arctic_user"},
            "text": text,
        },
    }


async def test_message_reaches_the_right_chat() -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = client(handler)
    assert await bot.send_message(42, "привет")
    await bot.aclose()

    assert seen["url"].endswith(f"/bot{TOKEN}/sendMessage")
    assert seen["body"]["chat_id"] == 42
    assert seen["body"]["text"] == "привет"


async def test_token_never_appears_in_the_body() -> None:
    """Токен уходит в пути, а не в теле: тела попадают в логи чаще."""
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = client(handler)
    await bot.send_message(42, "привет")
    await bot.aclose()

    assert TOKEN not in seen["json"]


async def test_updates_are_parsed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": [message(7, "/start")]})

    bot = client(handler)
    updates = await bot.get_updates(offset=0)
    await bot.aclose()

    assert updates == [
        Update(update_id=7, chat_id=42, user_id=42, username="arctic_user", text="/start")
    ]


async def test_offset_is_passed_along() -> None:
    """Без сдвига Telegram отдаёт те же апдейты снова и снова."""
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"ok": True, "result": []})

    bot = client(handler)
    await bot.get_updates(offset=100)
    await bot.aclose()

    assert seen["body"]["offset"] == 100


async def test_updates_without_text_are_skipped() -> None:
    """Стикеры, фото и служебные события боту не адресованы."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"update_id": 1, "message": {"chat": {"id": 42}, "sticker": {}}},
                    {"update_id": 2, "edited_message": {"chat": {"id": 42}, "text": "правка"}},
                    message(3, "нужное"),
                ],
            },
        )

    bot = client(handler)
    updates = await bot.get_updates(offset=0)
    await bot.aclose()

    assert [update.text for update in updates] == ["нужное"]


async def test_network_failure_is_survivable() -> None:
    """Обрыв связи — норма при long polling: цикл не должен на этом падать."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("telegram недоступен")

    bot = client(handler)
    assert await bot.get_updates(offset=0) == []
    assert not await bot.send_message(42, "привет")
    await bot.aclose()


async def test_refusal_from_telegram_is_not_an_exception() -> None:
    """Заблокировавший бота пользователь — обычное дело, а не сбой."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "bot was blocked by the user"})

    bot = client(handler)
    assert not await bot.send_message(42, "привет")
    await bot.aclose()


async def test_broken_answer_is_not_an_exception() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    bot = client(handler)
    assert await bot.get_updates(offset=0) == []
    await bot.aclose()

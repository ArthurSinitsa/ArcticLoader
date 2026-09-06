"""Проверка Turnstile — раздел 4.4 плана.

Гостевые квоты считаются по отпечатку из IP и User-Agent, а он меняется одним
кликом: инкогнито, VPN, другой браузер — и лимит начинается заново. Turnstile
отсекает автоматику до того, как она дойдёт до yt-dlp и потратит
IP-репутацию за всех.

Сеть не дёргаем: запросы к Cloudflare идут через подставной транспорт, зато
проверяется ровно то, что мы отправляем.
"""

from typing import Any

import httpx
import pytest

from app.services import turnstile

SECRET = "секрет-сайта"
TOKEN = "токен-из-виджета"


def transport(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_valid_token_is_accepted() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    async with transport(handler) as client:
        assert await turnstile.verify(secret=SECRET, token=TOKEN, client=client)


async def test_invalid_token_is_rejected() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error-codes": ["invalid-input"]})

    async with transport(handler) as client:
        assert not await turnstile.verify(secret=SECRET, token=TOKEN, client=client)


async def test_secret_and_token_reach_cloudflare() -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"success": True})

    async with transport(handler) as client:
        await turnstile.verify(secret=SECRET, token=TOKEN, ip="203.0.113.7", client=client)

    assert seen["url"] == turnstile.VERIFY_URL
    assert "secret=" in seen["body"]
    assert "remoteip=203.0.113.7" in seen["body"]


async def test_network_failure_means_refusal() -> None:
    """Если проверить некому — пропускать нельзя: именно на это и рассчитан бот."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cloudflare недоступен")

    async with transport(handler) as client:
        assert not await turnstile.verify(secret=SECRET, token=TOKEN, client=client)


async def test_broken_answer_means_refusal() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    async with transport(handler) as client:
        assert not await turnstile.verify(secret=SECRET, token=TOKEN, client=client)


async def test_server_error_means_refusal() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    async with transport(handler) as client:
        assert not await turnstile.verify(secret=SECRET, token=TOKEN, client=client)


@pytest.mark.parametrize("token", ["", "   "])
async def test_empty_token_never_reaches_the_network(token: str) -> None:
    """Пустой токен — заведомо не пропуск, тратить на него запрос незачем."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("запроса быть не должно")

    async with transport(handler) as client:
        assert not await turnstile.verify(secret=SECRET, token=token, client=client)

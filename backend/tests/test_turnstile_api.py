"""Turnstile на гостевой форме — раздел 4.4.

Проверка касается только гостей: у авторизованного есть учётка, лимиты роли и
имя в журнале — доказывать, что он человек, ему уже незачем.
"""

from typing import Any

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.services import turnstile
from tests.test_auth import add_user, login

DOWNLOAD: dict[str, Any] = {"url": "https://example.com/v.mp4", "quality": "720p"}
SITE_KEY = "0x-публичный-ключ"
SECRET_KEY = "0x-секретный-ключ"


def enable(settings: Settings) -> None:
    settings.turnstile_site_key = SITE_KEY
    settings.turnstile_secret_key = SECRET_KEY


def answer(monkeypatch: pytest.MonkeyPatch, verdict: bool) -> list[dict[str, Any]]:
    """Подменяет обращение к Cloudflare и запоминает, с чем его позвали."""
    calls: list[dict[str, Any]] = []

    async def verify(**kwargs: Any) -> bool:
        calls.append(kwargs)
        return verdict

    monkeypatch.setattr(turnstile, "verify", verify)
    return calls


async def test_without_keys_nothing_changes(client: AsyncClient) -> None:
    """Пока ключи не заведены, сервис работает как раньше — это же и dev-режим."""
    assert (await client.post("/api/downloads", json=DOWNLOAD)).status_code == 202


async def test_guest_without_token_is_refused(
    client: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable(settings)
    answer(monkeypatch, True)

    response = await client.post("/api/downloads", json=DOWNLOAD)

    assert response.status_code == 403


async def test_guest_with_valid_token_passes(
    client: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable(settings)
    answer(monkeypatch, True)

    response = await client.post("/api/downloads", json={**DOWNLOAD, "turnstile_token": "хороший"})

    assert response.status_code == 202


async def test_guest_with_rejected_token_is_refused(
    client: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable(settings)
    answer(monkeypatch, False)

    response = await client.post(
        "/api/downloads", json={**DOWNLOAD, "turnstile_token": "поддельный"}
    )

    assert response.status_code == 403


async def test_cloudflare_sees_the_secret_and_the_address(
    client: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable(settings)
    calls = answer(monkeypatch, True)

    await client.post("/api/downloads", json={**DOWNLOAD, "turnstile_token": "хороший"})

    assert calls[0]["secret"] == SECRET_KEY
    assert calls[0]["token"] == "хороший"
    # Адрес нужен Cloudflare, чтобы сверить его с тем, где решалась задача.
    assert calls[0]["ip"]


async def test_signed_in_user_is_not_asked(
    client: AsyncClient,
    settings: Settings,
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """У пользователя есть учётка и имя в журнале — капча ему ни к чему."""
    enable(settings)
    calls = answer(monkeypatch, False)
    await add_user(session_factory)
    await login(client)

    response = await client.post("/api/downloads", json=DOWNLOAD)

    assert response.status_code == 202
    assert calls == []


async def test_site_key_is_published_for_the_page(client: AsyncClient, settings: Settings) -> None:
    """Виджет рисуется по публичному ключу — фронт должен где-то его взять."""
    enable(settings)

    body = (await client.get("/api/config")).json()

    assert body["turnstile_site_key"] == SITE_KEY


async def test_page_learns_that_check_is_off(client: AsyncClient) -> None:
    body = (await client.get("/api/config")).json()

    assert body["turnstile_site_key"] == ""

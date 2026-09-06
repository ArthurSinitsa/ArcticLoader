"""Проверка Cloudflare Turnstile — раздел 4.4 плана.

Гостевая квота считается по отпечатку из IP и User-Agent, а он меняется одним
кликом: инкогнито, VPN, другой браузер — и лимит начинается заново. Turnstile
отсекает автоматику до того, как она дойдёт до yt-dlp: каждая гостевая
загрузка — это обращение к площадке с домашнего IP, и банят потом всех.

Трафик через Cloudflare не идёт: на страницу ставится виджет, сюда приходит
его токен, и мы задаём один вопрос — настоящий ли это посетитель.
"""

import logging

import httpx

log = logging.getLogger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

#: Проверка стоит перед постановкой задачи в очередь, поэтому ждать долго
#: нельзя: лучше отказать, чем держать пользователя.
TIMEOUT_SECONDS = 5.0


async def verify(
    *, secret: str, token: str, ip: str | None = None, client: httpx.AsyncClient | None = None
) -> bool:
    """Настоящий ли посетитель прислал токен.

    Отказ при любой неясности: недоступный Cloudflare, кривой ответ, пустой
    токен. Пропускать в такие моменты — значит открывать ровно ту дверь, ради
    которой проверка и ставилась.
    """
    if not token.strip():
        return False

    owned = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        payload = {"secret": secret, "response": token}
        if ip:
            # Cloudflare сверяет адрес с тем, с которого решалась задача.
            payload["remoteip"] = ip

        response = await http.post(VERIFY_URL, data=payload, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        verdict = bool(response.json().get("success"))
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("проверка Turnstile не удалась: %s", exc)
        return False
    finally:
        if owned:
            await http.aclose()

    if not verdict:
        log.info("Turnstile не признал посетителя настоящим")
    return verdict

"""Настройка логирования.

Отдельный тест из-за находки живой проверки: `httpx` пишет в лог полный URL
запроса, а у Telegram Bot API токен лежит именно в пути. В `docker compose
logs` он оказывался целиком.
"""

import logging

from app.logging_setup import HTTP_LOGGERS, configure_logging


def test_http_client_does_not_log_request_urls() -> None:
    """Токен бота живёт в пути запроса — `httpx` не должен писать URL в лог."""
    configure_logging("INFO")

    for name in HTTP_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_http_client_still_reports_problems() -> None:
    """Предупреждения и ошибки самого клиента остаются: они нужны в разборе."""
    configure_logging("INFO")

    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)


def test_application_logging_is_unaffected() -> None:
    configure_logging("INFO")

    assert logging.getLogger("app.services.ytdlp").isEnabledFor(logging.INFO)


def test_debug_level_still_silences_http_clients() -> None:
    """`LOG_LEVEL=DEBUG` включают, чтобы разобрать свою логику, а не чтобы
    вывалить секреты в консоль."""
    configure_logging("DEBUG")

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING

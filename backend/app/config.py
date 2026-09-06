"""Конфигурация сервиса: читается из переменных окружения и `.env`."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://arctic:arctic@db:5432/arctic"
    redis_url: str = "redis://redis:6379/0"

    media_root: Path = Path("/srv/media")
    public_files_base_url: str = "http://localhost:8080/files"

    # Глобальный семафор из раздела 3 плана: стоит выше любых пользовательских
    # квот и просто удерживает лишнее в очереди.
    max_concurrent_downloads: int = 4
    download_timeout_seconds: int = 1800

    #: Кэш списка форматов (раздел 3.1): открытие выпадающего списка не должно
    #: стоить обращения к площадке.
    url_meta_ttl_seconds: int = 1800

    #: Главная оптимизация раздела 1.5: progressive-форматы отдаются прямой
    #: ссылкой на CDN, сервер в передаче не участвует. Выключается, если
    #: площадка начнёт привязывать ссылки к IP.
    direct_links_enabled: bool = True

    #: Первая учётка (раздел 2.6): публичной регистрации нет, а бот с заявками
    #: появится только на Этапе 6 — без этого администрировать сервис некому.
    #: Пустой email означает «не создавать»; пароль обязателен к смене при
    #: первом входе.
    superadmin_email: str = ""
    superadmin_password: SecretStr = SecretStr("")
    superadmin_name: str = "Суперадминистратор"

    #: Срок жизни скачанных файлов (раздел 3). Диск ноутбука не резиновый:
    #: то, что не забрали за двое суток, почти наверняка уже не заберут.
    file_ttl_hours: int = 48
    #: Как часто просыпается чистка. Полчаса — компромисс: файлы не залёживаются,
    #: а лишних пробуждений воркера немного.
    cleanup_interval_minutes: int = 30
    #: Ниже этого порога новые загрузки не начинаются: место нужно и системе,
    #: и ремуксу, которому во время работы требуется второй файл.
    min_free_disk_bytes: int = 5 * 1024**3

    #: Telegram-бот (Этап 6): заявки на регистрацию, временные пароли,
    #: уведомления о готовности файла. Пустой токен означает, что бота нет —
    #: процесс просто завершается, остальной сервис работает как обычно.
    telegram_bot_token: str = ""
    #: Куда падают уведомления о новых заявках. Ноль — никуда: админ увидит
    #: их в очереди админки, просто не мгновенно.
    telegram_alert_chat_id: int = 0
    #: Сколько живёт временный пароль из бота — раздел 2.6.
    temporary_password_hours: int = 48

    #: Turnstile на гостевой форме (раздел 4.4). Пустой секрет выключает
    #: проверку целиком — так сервис работает локально и до получения ключей.
    #: Публичный ключ уходит на страницу, секретный остаётся здесь.
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""

    #: Сессии (раздел 4.4). Неделя со скользящим продлением: пользователь не
    #: должен вылетать посреди работы, но и вечная cookie не нужна.
    session_ttl_seconds: int = 7 * 24 * 3600
    session_cookie_name: str = "arctic_session"
    #: На проде за TLS — обязательно true; локально сервис работает по HTTP,
    #: и с secure=true браузер cookie просто не сохранит.
    session_cookie_secure: bool = False

    #: Защита входа от перебора (раздел 2.6): пять неудач по связке IP + логин
    #: закрывают вход на пятнадцать минут.
    login_max_attempts: int = 5
    login_block_seconds: int = 900

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()

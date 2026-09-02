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

    #: Рубильник анонимного скачивания из раздела 3.6. Волна ботов лечится
    #: переключением флага, а не ночным деплоем.
    guest_access_enabled: bool = True

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Конфигурация сервиса: читается из переменных окружения и `.env`."""

from functools import lru_cache
from pathlib import Path

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
    default_format: str = "bv*+ba/b"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()

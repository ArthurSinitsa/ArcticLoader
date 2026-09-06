"""DTO запросов и ответов API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.models import TaskStatus
from app.services.formats import DEFAULT_QUALITY, Quality


class DownloadCreate(BaseModel):
    url: HttpUrl
    #: Только профиль из раздела 2.7 — сырой селектор yt-dlp наружу не пускаем.
    quality: Quality = DEFAULT_QUALITY
    #: Ответ виджета Turnstile. Спрашивается только у гостей и только когда
    #: заведены ключи (раздел 4.4).
    turnstile_token: str | None = None


class DownloadCreated(BaseModel):
    task_id: uuid.UUID
    status: TaskStatus


class DownloadState(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_url: str
    status: TaskStatus
    quality: Quality
    progress: float
    title: str | None
    extractor: str | None
    error_code: str | None
    #: Сырой хвост stderr — для журнала и будущей админки.
    error_message: str | None
    attempts: int
    file_size: int | None
    created_at: datetime
    finished_at: datetime | None

    #: Заполняется только в статусе `ready`. Ведёт либо на Caddy, либо прямо
    #: на CDN источника, если файл не проходил через сервер (раздел 1.5).
    download_url: str | None = None
    #: True, когда сервер в передаче файла не участвовал.
    direct: bool = False
    #: Человеческое объяснение ошибки — то, что видит пользователь.
    error_hint: str | None = None


class QualityOptionOut(BaseModel):
    quality: Quality
    label: str
    height: int | None
    filesize: int | None


class FormatsResponse(BaseModel):
    title: str
    extractor: str
    duration: float | None
    options: list[QualityOptionOut]


class LoginRequest(BaseModel):
    #: Не `EmailStr`: валидация адреса — забота того, кто заводит учётку,
    #: а на входе лишний пакет ради проверки формата не нужен.
    email: str
    password: str


class PasswordChange(BaseModel):
    #: Не нужен при принудительной смене: человек только что вошёл этим самым
    #: паролем. При добровольной — обязателен, см. `change_password`.
    current_password: str | None = None
    #: Восемь символов — нижняя граница, ниже которой пароль перебирается
    #: быстрее, чем успеет сработать блокировка входа.
    new_password: str = Field(min_length=8)


class CurrentUser(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    must_change_password: bool
    role: str


class DownloadHistory(BaseModel):
    items: list[DownloadState]
    #: Всего загрузок у пользователя, а не на странице: иначе UI не покажет,
    #: что дальше есть ещё.
    total: int


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: str
    is_active: bool
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None


class UserPage(BaseModel):
    items: list[UserOut]
    total: int


class UserCreate(BaseModel):
    email: str
    name: str
    #: По умолчанию обычный пользователь: заводить админов может только
    #: суперадмин, и это проверяется отдельно.
    role: str = "user"


class UserCreated(BaseModel):
    user: UserOut
    #: Показывается ровно один раз — дальше в базе только хеш. Его сообщают
    #: человеку и требуют сменить при первом входе (раздел 2.6).
    temporary_password: str


class UserUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    #: Смена роли доступна только суперадмину — проверяется в обработчике.
    role: str | None = None


class QuotaOut(BaseModel):
    role: str
    title: str
    is_unlimited: bool
    daily_limit: int
    concurrent_limit: int
    bucket_capacity: int
    bucket_refill_minutes: int


class QuotaUpdate(BaseModel):
    daily_limit: int = Field(ge=0)
    concurrent_limit: int = Field(ge=0)
    bucket_capacity: int = Field(ge=0)
    #: Ноль означал бы деление на ноль при восполнении ведра.
    bucket_refill_minutes: int = Field(ge=1)


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: str | None
    payload: dict | None
    ip: str | None
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditOut]


class AdminTaskOut(BaseModel):
    id: uuid.UUID
    #: Email владельца; пусто у гостевой загрузки — учётки за ней нет.
    owner: str | None
    source_url: str
    title: str | None
    quality: str
    status: TaskStatus
    progress: float
    #: Номер процесса yt-dlp: по нему видно, что задача действительно живёт.
    worker_pid: int | None
    created_at: datetime


class AdminTaskPage(BaseModel):
    items: list[AdminTaskOut]
    total: int


class FlagUpdate(BaseModel):
    enabled: bool


class PublicConfig(BaseModel):
    """То, что странице нужно знать о сервере до входа."""

    #: Пустая строка означает, что проверка выключена и виджет не рисуется.
    turnstile_site_key: str


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    telegram_id: int
    telegram_username: str | None
    email: str
    name: str
    status: str
    created_at: datetime


class RequestPage(BaseModel):
    items: list[RequestOut]
    total: int

"""ORM-модели. Схема — раздел 2.4 плана."""

import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TaskStatus(StrEnum):
    QUEUED = "queued"
    EXTRACTING = "extracting"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


#: Из терминальных статусов задача уже не выходит: воркер, доехавший до записи
#: прогресса после отмены, не должен воскрешать задачу.
TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.READY,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.EXPIRED,
    }
)

task_status_enum = sa.Enum(
    TaskStatus,
    name="task_status",
    values_callable=lambda enum: [member.value for member in enum],
)


class Role(Base):
    """Роль из матрицы 2.5.1. Гость — такая же строка справочника, чтобы его
    лимиты правились из админки наравне с остальными."""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(16), unique=True)
    title: Mapped[str] = mapped_column(sa.String(64))
    #: Суперадмин: проверка квот не считает его, а пропускает.
    is_unlimited: Mapped[bool] = mapped_column(sa.Boolean, default=False)

    def __repr__(self) -> str:
        return f"<Role {self.code}>"


class QuotaProfile(Base):
    """Лимиты роли — таблица 2.5.2.

    Отдельная таблица, а не константы в коде: правка цифр не должна требовать
    пересборки (раздел 6), а на Этапе 5 их правит суперадмин из интерфейса.
    """

    __tablename__ = "quota_profiles"

    role_id: Mapped[int] = mapped_column(
        sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    daily_limit: Mapped[int] = mapped_column(sa.Integer)
    concurrent_limit: Mapped[int] = mapped_column(sa.Integer)
    #: Ёмкость ведра — сколько загрузок можно запустить пачкой.
    bucket_capacity: Mapped[int] = mapped_column(sa.Integer)
    #: Через сколько минут возвращается один токен.
    bucket_refill_minutes: Mapped[int] = mapped_column(sa.Integer)

    def __repr__(self) -> str:
        return f"<QuotaProfile role={self.role_id} daily={self.daily_limit}>"


class User(Base):
    """Учётная запись — раздел 2.4.

    Публичной регистрации нет: учётки заводит админ или бот по заявке
    (раздел 2.6), поэтому пароль всегда временный до первой смены.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(sa.String(255), unique=True)
    name: Mapped[str] = mapped_column(sa.String(128))
    #: Канал для сброса пароля и уведомлений о готовности файла (Этап 6).
    telegram_id: Mapped[int | None] = mapped_column(sa.BigInteger, unique=True)
    password_hash: Mapped[str] = mapped_column(sa.String(255))
    role_id: Mapped[int] = mapped_column(sa.ForeignKey("roles.id"))
    #: Пока True, доступ есть только к смене пароля (раздел 2.6).
    must_change_password: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)

    #: Пусто у гостя — тогда субъект опознаётся по отпечатку.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    #: Гостевой отпечаток: связка IP и User-Agent (раздел 4.4). За общим NAT
    #: одного IP мало — все пользователи офиса делили бы одну квоту.
    guest_fingerprint: Mapped[str | None] = mapped_column(sa.String(64), index=True)

    source_url: Mapped[str] = mapped_column(sa.Text)
    extractor: Mapped[str | None] = mapped_column(sa.String(64))
    #: Профиль качества из раздела 2.7 — то, что выбрал пользователь.
    quality: Mapped[str] = mapped_column(sa.String(16))
    #: Селектор yt-dlp, в который профиль развернулся. Только для разбора полётов.
    format_id: Mapped[str | None] = mapped_column(sa.String(128))
    title: Mapped[str | None] = mapped_column(sa.Text)

    status: Mapped[TaskStatus] = mapped_column(
        task_status_enum, default=TaskStatus.QUEUED, index=True
    )
    progress: Mapped[float] = mapped_column(sa.Float, default=0.0)
    worker_pid: Mapped[int | None] = mapped_column(sa.Integer)

    error_code: Mapped[str | None] = mapped_column(sa.String(32))
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    #: Номер попытки: ретраятся только сетевые сбои и таймауты (раздел 3.4).
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0)

    file_path: Mapped[str | None] = mapped_column(sa.Text)
    file_size: Mapped[int | None] = mapped_column(sa.BigInteger)
    #: Ссылка на CDN источника, когда файл качается мимо сервера (раздел 1.5).
    #: Заполнена вместо file_path — эти два поля взаимоисключающие.
    direct_url: Mapped[str | None] = mapped_column(sa.Text)

    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.status} {self.source_url!r}>"


class AuditEntry(Base):
    """Журнал административных действий — раздел 3.6.

    Кто, что, над кем, когда и с какого адреса. Не бюрократия: когда учётка
    или чужая задача исчезнет, объяснить произошедшее больше нечем.
    """

    __tablename__ = "audit_log"

    #: Порядковый номер, а не UUID: журнал — лента, и читается она по порядку.
    #: Случайный идентификатор не даёт устойчивой сортировки для записей,
    #: сделанных в одну и ту же секунду.
    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    #: Пусто, если действие совершил сам сервис, а не человек.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(sa.String(64), index=True)
    target_type: Mapped[str] = mapped_column(sa.String(32))
    target_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    #: Подробности действия: что именно изменилось и на что.
    payload: Mapped[dict | None] = mapped_column(sa.JSON)
    ip: Mapped[str | None] = mapped_column(sa.String(45))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<AuditEntry {self.action} {self.target_type}:{self.target_id}>"


class RuntimeFlag(Base):
    """Глобальный рубильник — раздел 3.6.

    В БД, а не в `.env`: волна ботов лечится галочкой, а не перезапуском
    контейнера ночью. Строки заводятся по мере переключения, отсутствие
    записи означает значение по умолчанию.
    """

    __tablename__ = "runtime_flags"

    key: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(sa.Boolean)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )

    def __repr__(self) -> str:
        return f"<RuntimeFlag {self.key}={self.enabled}>"


class UrlMeta(Base):
    """Кэш метаданных ссылки — раздел 3.1.

    Открытие выпадающего списка качества не должно стоить обращения к
    площадке: один пользователь, перебирающий варианты, иначе тратит
    IP-репутацию за всех.
    """

    __tablename__ = "url_meta"

    url_hash: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    source_url: Mapped[str] = mapped_column(sa.Text)
    extractor: Mapped[str] = mapped_column(sa.String(64))
    #: Сериализованный MediaInfo без ссылок на CDN: они живут часы и весят
    #: килобайты на формат.
    payload: Mapped[dict] = mapped_column(sa.JSON)
    #: Индекс нужен чистке протухшего кэша: она ходит именно по времени.
    fetched_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<UrlMeta {self.source_url!r} {self.fetched_at}>"

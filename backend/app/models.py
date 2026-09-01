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


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)

    # FK на users появится миграцией Этапа 4 — таблицы пользователей ещё нет.
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True)
    guest_fingerprint: Mapped[str | None] = mapped_column(sa.String(64), index=True)

    source_url: Mapped[str] = mapped_column(sa.Text)
    extractor: Mapped[str | None] = mapped_column(sa.String(64))
    format_id: Mapped[str | None] = mapped_column(sa.String(128))
    title: Mapped[str | None] = mapped_column(sa.Text)

    status: Mapped[TaskStatus] = mapped_column(
        task_status_enum, default=TaskStatus.QUEUED, index=True
    )
    progress: Mapped[float] = mapped_column(sa.Float, default=0.0)
    worker_pid: Mapped[int | None] = mapped_column(sa.Integer)

    error_code: Mapped[str | None] = mapped_column(sa.String(32))
    error_message: Mapped[str | None] = mapped_column(sa.Text)

    file_path: Mapped[str | None] = mapped_column(sa.Text)
    file_size: Mapped[int | None] = mapped_column(sa.BigInteger)

    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.status} {self.source_url!r}>"

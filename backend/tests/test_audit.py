"""Журнал аудита — раздел 3.6 плана.

Раз в системе есть удаление учётных записей и прерывание чужих задач, все
административные действия должны оставлять след: когда что-то пропадёт,
единственный способ понять причину — журнал.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditEntry
from app.services import audit


async def test_action_is_recorded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    actor = uuid.uuid4()
    target = uuid.uuid4()

    async with session_factory() as session:
        await audit.record(
            session,
            actor_id=actor,
            action="user.delete",
            target_type="user",
            target_id=str(target),
            payload={"email": "кто-то@example.com"},
            ip="203.0.113.7",
        )

        entry = await session.scalar(sa.select(AuditEntry))

    assert entry.actor_id == actor
    assert entry.action == "user.delete"
    assert entry.target_id == str(target)
    assert entry.payload["email"] == "кто-то@example.com"
    assert entry.ip == "203.0.113.7"
    assert entry.created_at is not None


async def test_entries_come_newest_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Журнал читают, когда что-то случилось: свежее важнее старого."""
    async with session_factory() as session:
        for index in range(3):
            await audit.record(
                session, actor_id=None, action=f"действие.{index}", target_type="user"
            )

        entries = await audit.recent(session, limit=10)

    assert [entry.action for entry in entries] == ["действие.2", "действие.1", "действие.0"]


async def test_journal_page_is_limited(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        for index in range(5):
            await audit.record(session, actor_id=None, action=f"a{index}", target_type="user")

        entries = await audit.recent(session, limit=2)

    assert len(entries) == 2


async def test_actor_may_be_unknown(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Действие мог совершить не человек, а сам сервис — запись всё равно нужна."""
    async with session_factory() as session:
        await audit.record(session, actor_id=None, action="task.expire", target_type="task")

        entry = await session.scalar(sa.select(AuditEntry))

    assert entry.actor_id is None

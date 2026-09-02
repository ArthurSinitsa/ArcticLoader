"""Справочник ролей и их лимитов — таблицы 2.5.1 и 2.5.2 плана.

Числа ниже — стартовые значения, а не источник истины. После первого запуска
лимиты живут в `quota_profiles` и правятся из админки (Этап 5), поэтому
`ensure_roles` заполняет пустые места и никогда не переписывает существующие
строки: иначе перезапуск API откатывал бы вчерашнюю правку суперадмина.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QuotaProfile, Role
from app.services.quotas import Quota

log = logging.getLogger(__name__)


class RoleCode(StrEnum):
    GUEST = "guest"
    USER = "user"
    ADMIN = "admin"
    SUPERADMIN = "superadmin"


@dataclass(frozen=True)
class _RoleSeed:
    code: RoleCode
    title: str
    is_unlimited: bool
    daily_limit: int
    concurrent_limit: int
    bucket_capacity: int
    bucket_refill_minutes: int


#: Таблица 2.5.2. Суперадмин лимитов не имеет, но строка ему нужна: без неё
#: пришлось бы городить исключение в каждом запросе к справочнику.
DEFAULT_ROLES: tuple[_RoleSeed, ...] = (
    _RoleSeed(RoleCode.GUEST, "Гость", False, 5, 1, 1, 30),
    _RoleSeed(RoleCode.USER, "Пользователь", False, 16, 4, 4, 20),
    _RoleSeed(RoleCode.ADMIN, "Администратор", False, 24, 4, 4, 10),
    _RoleSeed(RoleCode.SUPERADMIN, "Суперадминистратор", True, 0, 0, 0, 1),
)


async def ensure_roles(session: AsyncSession) -> None:
    """Завести отсутствующие роли и их квоты. Идемпотентно."""
    existing = set((await session.scalars(sa.select(Role.code))).all())

    created = 0
    for seed in DEFAULT_ROLES:
        if seed.code.value in existing:
            continue
        role = Role(code=seed.code.value, title=seed.title, is_unlimited=seed.is_unlimited)
        session.add(role)
        await session.flush()
        session.add(
            QuotaProfile(
                role_id=role.id,
                daily_limit=seed.daily_limit,
                concurrent_limit=seed.concurrent_limit,
                bucket_capacity=seed.bucket_capacity,
                bucket_refill_minutes=seed.bucket_refill_minutes,
            )
        )
        created += 1

    await session.commit()
    if created:
        log.info("справочник ролей дополнен: %s новых", created)


async def load_quota(session: AsyncSession, code: RoleCode | str) -> Quota:
    """Лимиты роли из БД — то, чем оперирует `quotas.consume`."""
    row = (
        await session.execute(
            sa.select(QuotaProfile, Role.is_unlimited)
            .join(Role, Role.id == QuotaProfile.role_id)
            .where(Role.code == str(code))
        )
    ).first()

    if row is None:
        raise LookupError(f"нет квот для роли {code}")

    profile, is_unlimited = row
    return _to_quota(profile, is_unlimited)


def _to_quota(profile: QuotaProfile, is_unlimited: bool) -> Quota:
    return Quota(
        daily_limit=profile.daily_limit,
        concurrent_limit=profile.concurrent_limit,
        bucket_capacity=profile.bucket_capacity,
        bucket_refill_minutes=profile.bucket_refill_minutes,
        unlimited=is_unlimited,
    )


async def load_quota_by_role_id(session: AsyncSession, role_id: int) -> tuple[RoleCode, Quota]:
    """Код роли и её лимиты одним запросом — то, что нужно на каждый запрос
    авторизованного пользователя."""
    row = (
        await session.execute(
            sa.select(Role.code, Role.is_unlimited, QuotaProfile)
            .join(QuotaProfile, QuotaProfile.role_id == Role.id)
            .where(Role.id == role_id)
        )
    ).first()

    if row is None:
        raise LookupError(f"нет квот для роли {role_id}")

    code, is_unlimited, profile = row
    return RoleCode(code), _to_quota(profile, is_unlimited)

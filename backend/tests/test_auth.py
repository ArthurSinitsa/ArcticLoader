"""Вход, выход и смена пароля — разделы 2.6 и 4.4 плана."""

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Role, User
from app.security import hash_password, verify_password
from app.services.roles import RoleCode

EMAIL = "user@example.com"
PASSWORD = "правильный-пароль"
NEW_PASSWORD = "другой-пароль"


async def add_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email: str = EMAIL,
    password: str = PASSWORD,
    role: RoleCode = RoleCode.USER,
    must_change_password: bool = False,
    is_active: bool = True,
) -> User:
    async with session_factory() as session:
        role_id = await session.scalar(sa.select(Role.id).where(Role.code == role.value))
        user = User(
            email=email,
            name="Тестовый",
            password_hash=hash_password(password),
            role_id=role_id,
            must_change_password=must_change_password,
            is_active=is_active,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def login(client: AsyncClient, *, email: str = EMAIL, password: str = PASSWORD):
    return await client.post("/api/auth/login", json={"email": email, "password": password})


async def test_correct_password_opens_a_session(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    await add_user(session_factory)

    response = await login(client)

    assert response.status_code == 204
    assert settings.session_cookie_name in response.cookies


async def test_session_cookie_is_hidden_from_scripts(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """XSS не должен уносить сессию — cookie только для браузера."""
    await add_user(session_factory)

    response = await login(client)

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


async def test_wrong_password_is_refused(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    await add_user(session_factory)

    response = await login(client, password="не тот")

    assert response.status_code == 401
    assert settings.session_cookie_name not in response.cookies


async def test_unknown_email_answers_the_same_way(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Разный ответ на «нет такого» и «пароль не тот» выдаёт список учёток."""
    await add_user(session_factory)

    unknown = await login(client, email="никого@example.com")
    wrong = await login(client, password="не тот")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


async def test_disabled_account_cannot_log_in(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory, is_active=False)

    response = await login(client)

    assert response.status_code == 403


async def test_login_records_the_time(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory)

    await login(client)

    async with session_factory() as session:
        user = await session.scalar(sa.select(User).where(User.email == EMAIL))
    assert user.last_login_at is not None


async def test_fifth_wrong_password_blocks_the_door(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory)
    for _ in range(5):
        await login(client, password="не тот")

    # Даже верный пароль теперь не проходит — блокировка по связке IP + логин.
    response = await login(client)

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


async def test_successful_login_clears_the_counter(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Опечатки прошлой недели не должны копиться до блокировки."""
    await add_user(session_factory)
    for _ in range(4):
        await login(client, password="не тот")
    await login(client)

    for _ in range(4):
        await login(client, password="не тот")

    assert (await login(client)).status_code == 204


async def test_me_needs_a_session(client: AsyncClient) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_me_tells_who_you_are(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory)
    await login(client)

    body = (await client.get("/api/auth/me")).json()

    assert body["email"] == EMAIL
    assert body["role"] == RoleCode.USER
    assert body["must_change_password"] is False


async def test_logout_ends_the_session(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory)
    await login(client)

    await client.post("/api/auth/logout")

    assert (await client.get("/api/auth/me")).status_code == 401


async def test_password_change_requires_the_old_one(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Иначе уведённая cookie превращается в захват учётки."""
    await add_user(session_factory)
    await login(client)

    response = await client.post(
        "/api/auth/password",
        json={"current_password": "не тот", "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 401


async def test_short_password_is_refused(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory)
    await login(client)

    response = await client.post(
        "/api/auth/password", json={"current_password": PASSWORD, "new_password": "коротк"}
    )

    assert response.status_code == 422


async def test_password_change_takes_effect(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory, must_change_password=True)
    await login(client)

    response = await client.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 204
    async with session_factory() as session:
        user = await session.scalar(sa.select(User).where(User.email == EMAIL))
    assert verify_password(NEW_PASSWORD, user.password_hash)
    assert not user.must_change_password


async def test_current_device_survives_the_password_change(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Смена пароля не должна выкидывать того, кто её только что сделал."""
    await add_user(session_factory)
    await login(client)

    await client.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert (await client.get("/api/auth/me")).status_code == 200


async def test_temporary_password_blocks_everything_else(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`must_change_password` закрывает сервис до смены пароля (раздел 2.6)."""
    await add_user(session_factory, must_change_password=True)
    await login(client)

    response = await client.post(
        "/api/downloads", json={"url": "https://example.com/v.mp4", "quality": "720p"}
    )

    assert response.status_code == 403


async def test_block_message_rounds_minutes_up(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Пятнадцать минут блокировки не должны выглядеть как шестнадцать."""
    await add_user(session_factory)
    for _ in range(5):
        await login(client, password="не тот")

    detail = (await login(client)).json()["detail"]

    assert f"{detail['retry_after'] // 60} мин" in detail["message"]


async def test_temporary_password_change_needs_no_old_one(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Человек только что вошёл этим паролем — второй раз спрашивать незачем."""
    await add_user(session_factory, must_change_password=True)
    await login(client)

    response = await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})

    assert response.status_code == 204
    async with session_factory() as session:
        user = await session.scalar(sa.select(User).where(User.email == EMAIL))
    assert verify_password(NEW_PASSWORD, user.password_hash)


async def test_voluntary_change_still_needs_the_old_one(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Иначе уведённая cookie превращается в захват учётки: вор меняет пароль
    и выкидывает владельца."""
    await add_user(session_factory)
    await login(client)

    response = await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})

    assert response.status_code == 401


async def test_wrong_old_password_is_still_refused_after_forced_change(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Смена сняла флаг — дальше учётка живёт по обычным правилам."""
    await add_user(session_factory, must_change_password=True)
    await login(client)
    await client.post("/api/auth/password", json={"new_password": NEW_PASSWORD})

    response = await client.post(
        "/api/auth/password",
        json={"current_password": "не тот", "new_password": "ещё-один-пароль"},
    )

    assert response.status_code == 401

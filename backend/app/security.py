"""Пароли.

Argon2id с умолчаниями библиотеки: параметры стойкости — не то место, где
стоит проявлять самостоятельность. Соль и параметры лежат внутри самой строки
хеша, поэтому отдельных колонок в `users` под них нет.
"""

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Проверка без исключений наружу: вызывающему нужен ответ да/нет."""
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError, VerificationError:
        return False

"""Работа с каталогом загрузок.

Том `MEDIA_ROOT` общий с Caddy, поэтому всё, что попадает в имена файлов,
предварительно санируется: заголовки с площадок содержат произвольные символы.
"""

import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import quote

_UNSAFE_CHARS = re.compile(r"[^\w.\- ]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

FALLBACK_FILENAME = "video"


def safe_filename(name: str, max_length: int = 120) -> str:
    """Приводит произвольный заголовок к безопасному имени файла."""
    cleaned = _UNSAFE_CHARS.sub(" ", name)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" .")
    return cleaned[:max_length].strip() or FALLBACK_FILENAME


def task_dir(media_root: Path, task_id: uuid.UUID | str) -> Path:
    """Отдельный каталог на задачу: так проще удалять частичные файлы."""
    return media_root / str(task_id)


def largest_file(directory: Path) -> Path | None:
    """Самый большой готовый файл в каталоге.

    Запасной путь на случай, если yt-dlp не напечатал итоговый путь: временные
    файлы (`.part`, `.ytdl`) отбрасываются.
    """
    candidates = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix not in (".part", ".ytdl")
    ]
    return max(candidates, key=lambda path: path.stat().st_size, default=None)


def public_url(base_url: str, media_root: Path, file_path: Path) -> str:
    """Ссылка, по которой файл отдаёт Caddy — приложение в передаче не участвует."""
    relative = file_path.relative_to(media_root)
    return f"{base_url.rstrip('/')}/{quote(relative.as_posix())}"


def free_space(path: Path) -> int:
    """Свободно байт на томе с загрузками — раздел 3 плана.

    Каталога может ещё не быть (первый запуск, том только смонтировали),
    поэтому поднимаемся к ближайшему существующему предку: место всё равно
    считается по тому целиком.
    """
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def remove_task_dir(media_root: Path, task_id: uuid.UUID | str) -> None:
    """Удалить файлы задачи. Отсутствие каталога — норма: файл могли снести
    руками, а чистка не должна на этом спотыкаться."""
    shutil.rmtree(task_dir(media_root, task_id), ignore_errors=True)

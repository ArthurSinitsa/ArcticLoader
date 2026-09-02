import uuid
from pathlib import Path
from urllib.parse import unquote

import pytest

from app.services import storage


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Обычное название", "Обычное название"),
        # Разделители пути обязаны исчезнуть: том общий с Caddy.
        ("../../etc/passwd", "etc passwd"),
        ("a/b\\c:d*e?f", "a b c d e f"),
        ("   ...   ", "video"),
        ("", "video"),
        ("Видео   №1", "Видео 1"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert storage.safe_filename(raw) == expected


def test_safe_filename_truncates() -> None:
    assert len(storage.safe_filename("я" * 500, max_length=32)) == 32


def test_task_dir_is_per_task(tmp_path: Path) -> None:
    assert storage.task_dir(tmp_path, "abc") == tmp_path / "abc"


def test_largest_file_ignores_partial_downloads(tmp_path: Path) -> None:
    (tmp_path / "small.mp4").write_bytes(b"x" * 10)
    (tmp_path / "big.mp4").write_bytes(b"x" * 100)
    (tmp_path / "huge.mp4.part").write_bytes(b"x" * 1000)

    assert storage.largest_file(tmp_path) == tmp_path / "big.mp4"


def test_largest_file_returns_none_for_empty_dir(tmp_path: Path) -> None:
    assert storage.largest_file(tmp_path) is None


def test_public_url_strips_media_root(tmp_path: Path) -> None:
    file_path = tmp_path / "task-1" / "video.mp4"

    url = storage.public_url("http://host:8080/files/", tmp_path, file_path)

    assert url == "http://host:8080/files/task-1/video.mp4"


def test_public_url_percent_encodes_names(tmp_path: Path) -> None:
    file_path = tmp_path / "task-1" / "моё видео.mp4"

    url = storage.public_url("http://host:8080/files", tmp_path, file_path)

    assert url.startswith("http://host:8080/files/task-1/")
    assert " " not in url
    assert unquote(url).endswith("task-1/моё видео.mp4")


def test_free_space_is_reported(tmp_path: Path) -> None:
    assert storage.free_space(tmp_path) > 0


def test_free_space_survives_missing_directory(tmp_path: Path) -> None:
    """Каталог загрузок может ещё не существовать — проверка места не должна
    падать раньше, чем воркер успеет его создать."""
    assert storage.free_space(tmp_path / "нет" / "такого") > 0


def test_task_directory_is_removed(tmp_path: Path) -> None:
    task_id = uuid.uuid4()
    directory = storage.task_dir(tmp_path, task_id)
    directory.mkdir(parents=True)
    (directory / "video.mp4").write_bytes(b"data")

    storage.remove_task_dir(tmp_path, task_id)

    assert not directory.exists()


def test_removing_absent_directory_is_not_an_error(tmp_path: Path) -> None:
    """Файл могли удалить руками — чистка не должна на этом спотыкаться."""
    storage.remove_task_dir(tmp_path, uuid.uuid4())

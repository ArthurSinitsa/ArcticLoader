"""Обёртка над yt-dlp.

Движок вызывается как внешний процесс, а не как Python-библиотека. Причины:

* у процесса есть настоящий PID — прерывание задач по разделу 2.5.3 плана
  (`SIGTERM`, через 5 секунд `SIGKILL`) работает буквально, без хаков;
* падение или зависание yt-dlp не роняет и не блокирует async-воркер;
* прогресс не теряется: `--progress-template` печатает машиночитаемые строки,
  которые на Этапе 2 уйдут прямо в Redis pub/sub.

Свои экстракторы не пишем — это раздел 6 плана.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.services import storage

log = logging.getLogger(__name__)

YTDLP_BINARY = "yt-dlp"

PROGRESS_PREFIX = "#ARCTIC_PROGRESS#"
FILE_PREFIX = "#ARCTIC_FILE#"

_PROGRESS_TEMPLATE = (
    f"download:{PROGRESS_PREFIX}"
    "%(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s"
    "|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s"
)
_FILEPATH_TEMPLATE = f"after_move:{FILE_PREFIX}%(filepath)s"
_OUTPUT_TEMPLATE = "%(title).150B.%(ext)s"

TERMINATE_GRACE_SECONDS = 5.0
_STDERR_TAIL_CHARS = 2000
_MISSING_VALUES = frozenset({"", "NA", "None", "none"})

ERROR_UNKNOWN = "unknown"
ERROR_TIMEOUT = "timeout"

#: Классификация ошибок из раздела 3.4 плана: реакция на них разная, поэтому
#: сваливать всё в «не получилось» нельзя. Порядок значим — первое совпадение
#: побеждает, а «Sign in to confirm you're not a bot» у YouTube означает
#: проблему с IP, а не отсутствие доступа.
ERROR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"no space left on device", re.I), "disk_full"),
    (re.compile(r"unsupported url|no suitable infoextractor", re.I), "unsupported_site"),
    (
        re.compile(
            r"http error 429|too many requests|rate.?limit"
            r"|confirm you.?re not a bot|captcha",
            re.I,
        ),
        "rate_limited",
    ),
    (
        # У YouTube две формулировки: «is not available in your country» и
        # «uploader has not made this video available in your country».
        re.compile(r"available in your country|geo.?restrict|blocked in your country", re.I),
        "geo_blocked",
    ),
    (
        re.compile(
            r"sign in|log in to|login required|private video|members-only"
            r"|requires authentication|age.?restrict",
            re.I,
        ),
        "login_required",
    ),
    (
        re.compile(
            r"unable to download|connection|timed out|name resolution|http error \d{3}",
            re.I,
        ),
        "network",
    ),
)


class YtDlpError(RuntimeError):
    """Ошибка движка с кодом из раздела 3.4 плана."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class MediaInfo:
    title: str
    extractor: str
    duration: float | None = None
    filesize_approx: int | None = None


@dataclass(frozen=True, slots=True)
class Progress:
    status: str
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed: float | None = None
    eta: int | None = None

    @property
    def percent(self) -> float | None:
        if not self.downloaded_bytes or not self.total_bytes:
            return None
        return min(100.0, self.downloaded_bytes / self.total_bytes * 100)


ProgressCallback = Callable[[Progress], Awaitable[None]]
StartCallback = Callable[[int], Awaitable[None]]


def classify_error(stderr: str) -> str:
    for pattern, code in ERROR_PATTERNS:
        if pattern.search(stderr):
            return code
    return ERROR_UNKNOWN


def parse_progress_line(line: str) -> Progress | None:
    """Разбирает строку `--progress-template`; всё прочее игнорируется."""
    if not line.startswith(PROGRESS_PREFIX):
        return None

    fields = line[len(PROGRESS_PREFIX) :].split("|")
    if len(fields) != 6:
        log.debug("не разобрана строка прогресса: %r", line)
        return None

    status, downloaded, total, estimate, speed, eta = fields
    return Progress(
        status=status,
        downloaded_bytes=_as_int(downloaded),
        # total_bytes известен не всегда — у DASH-фрагментов есть только оценка.
        total_bytes=_as_int(total) or _as_int(estimate),
        speed=_as_float(speed),
        eta=_as_int(eta),
    )


async def probe(url: str, *, timeout: float) -> MediaInfo:
    """Метаданные без скачивания: `yt-dlp -J`."""
    stdout, stderr, code = await _run(
        [YTDLP_BINARY, "-J", "--no-playlist", "--no-warnings", url], timeout=timeout
    )
    if code != 0:
        raise YtDlpError(classify_error(stderr), _tail(stderr))

    try:
        info = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise YtDlpError(ERROR_UNKNOWN, f"yt-dlp вернул неразбираемый JSON: {exc}") from exc

    return MediaInfo(
        title=info.get("title") or "video",
        extractor=info.get("extractor_key") or info.get("extractor") or "generic",
        duration=info.get("duration"),
        filesize_approx=info.get("filesize_approx"),
    )


async def download(
    url: str,
    *,
    format_spec: str,
    dest_dir: Path,
    timeout: float,
    on_start: StartCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Качает файл в `dest_dir` и возвращает путь к результату."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    command = [
        YTDLP_BINARY,
        "--newline",
        "--no-playlist",
        "--no-quiet",
        "--progress",
        "--no-simulate",
        "--restrict-filenames",
        "--progress-template",
        _PROGRESS_TEMPLATE,
        "--print",
        _FILEPATH_TEMPLATE,
        "--merge-output-format",
        "mp4",
        "-f",
        format_spec,
        "-o",
        str(dest_dir / _OUTPUT_TEMPLATE),
        url,
    ]

    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    log.info("yt-dlp запущен, pid=%s, формат=%s", process.pid, format_spec)
    if on_start is not None:
        await on_start(process.pid)

    printed_paths: list[str] = []
    try:
        async with asyncio.timeout(timeout):
            _, stderr_bytes, code = await asyncio.gather(
                _consume_stdout(process, printed_paths, on_progress),
                process.stderr.read(),
                process.wait(),
            )
    except TimeoutError:
        await terminate_process(process)
        raise YtDlpError(ERROR_TIMEOUT, f"превышен таймаут {timeout:.0f} с") from None

    if code != 0:
        stderr = stderr_bytes.decode(errors="replace")
        error_code = classify_error(stderr)
        log.error("yt-dlp завершился с кодом %s (%s)", code, error_code)
        raise YtDlpError(error_code, _tail(stderr))

    return _resolve_output(dest_dir, printed_paths)


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    """Мягкая остановка по процедуре из раздела 2.5.3: SIGTERM, затем SIGKILL."""
    if process.returncode is not None:
        return

    log.info("останавливаю процесс %s", process.pid)
    process.terminate()
    try:
        async with asyncio.timeout(TERMINATE_GRACE_SECONDS):
            await process.wait()
    except TimeoutError:
        log.warning("процесс %s не ответил на SIGTERM, отправляю SIGKILL", process.pid)
        process.kill()
        await process.wait()


async def _consume_stdout(
    process: asyncio.subprocess.Process,
    printed_paths: list[str],
    on_progress: ProgressCallback | None,
) -> None:
    async for raw_line in process.stdout:
        line = raw_line.decode(errors="replace").strip()
        if line.startswith(FILE_PREFIX):
            printed_paths.append(line[len(FILE_PREFIX) :])
        elif on_progress is not None and (progress := parse_progress_line(line)):
            await on_progress(progress)


def _resolve_output(dest_dir: Path, printed_paths: list[str]) -> Path:
    for raw_path in reversed(printed_paths):
        path = Path(raw_path)
        if path.is_file():
            return path

    fallback = storage.largest_file(dest_dir)
    if fallback is None:
        raise YtDlpError(ERROR_UNKNOWN, "yt-dlp отчитался об успехе, но файла нет")

    log.warning("итоговый путь не напечатан, взят крупнейший файл: %s", fallback.name)
    return fallback


async def _run(command: list[str], *, timeout: float) -> tuple[str, str, int]:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        await terminate_process(process)
        raise YtDlpError(ERROR_TIMEOUT, f"превышен таймаут {timeout:.0f} с") from None

    return stdout.decode(errors="replace"), stderr.decode(errors="replace"), process.returncode


def _as_int(value: str) -> int | None:
    number = _as_float(value)
    return None if number is None else int(number)


def _as_float(value: str) -> float | None:
    if value.strip() in _MISSING_VALUES:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _tail(stderr: str) -> str:
    return stderr.strip()[-_STDERR_TAIL_CHARS:] or "yt-dlp не вернул описание ошибки"

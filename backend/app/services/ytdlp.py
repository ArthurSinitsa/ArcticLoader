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
from dataclasses import dataclass, replace
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
#: Ремукс идёт без единого события прогресса, поэтому о его начале узнаём
#: отдельно: иначе полоска молча стоит на последних процентах, пока работает
#: ffmpeg, и выглядит это как зависание.
POST_PREFIX = "#ARCTIC_POST#"
_POSTPROCESS_TEMPLATE = f"postprocess:{POST_PREFIX}%(progress.status)s|%(progress.postprocessor)s"

_FILEPATH_TEMPLATE = f"after_move:{FILE_PREFIX}%(filepath)s"
_OUTPUT_TEMPLATE = "%(title).150B.%(ext)s"

TERMINATE_GRACE_SECONDS = 5.0
_STDERR_TAIL_CHARS = 2000
_MISSING_VALUES = frozenset({"", "NA", "None", "none"})

ERROR_UNKNOWN = "unknown"
ERROR_TIMEOUT = "timeout"

#: Так yt-dlp помечает отсутствующую дорожку в формате.
NO_CODEC = "none"

#: Протоколы, ссылку на которые можно отдать браузеру как есть. HLS и DASH
#: сюда не входят: манифест сам по себе не файл.
DIRECT_PROTOCOLS = frozenset({"http", "https"})

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
    # 404 и 410 — не сетевой сбой: повторять их бессмысленно, ссылки просто нет.
    (re.compile(r"http error 40[49]|not found|no longer available", re.I), "not_found"),
    (
        re.compile(
            r"unable to download|connection|timed out|name resolution|http error \d{3}",
            re.I,
        ),
        "network",
    ),
)

#: Повторять имеет смысл только то, что чинится само. Раздел 3.4: при
#: geo_blocked и login_required ретрай бесполезен, а rate_limited — сигнал о
#: проблемах с IP, и долбить площадку в этот момент только хуже.
RETRYABLE_ERRORS = frozenset({"network", ERROR_TIMEOUT})

#: Тексты для пользователя: «не получилось» — это не сообщение об ошибке.
USER_MESSAGES: dict[str, str] = {
    "unsupported_site": "Не удалось распознать видео на этой странице.",
    "not_found": "Видео не найдено: ссылка битая или запись удалена.",
    "login_required": "Видео закрыто — нужен вход или подписка.",
    "geo_blocked": "Видео недоступно в этой стране.",
    "rate_limited": "Площадка временно ограничила доступ. Попробуй позже.",
    "network": "Сеть подвела, связаться с площадкой не вышло.",
    "disk_full": "На сервере закончилось место.",
    ERROR_TIMEOUT: "Загрузка не уложилась в отведённое время.",
    ERROR_UNKNOWN: "Скачать не удалось, подробности — в журнале сервера.",
}


def user_message(error_code: str | None) -> str | None:
    if error_code is None:
        return None
    return USER_MESSAGES.get(error_code, USER_MESSAGES[ERROR_UNKNOWN])


class YtDlpError(RuntimeError):
    """Ошибка движка с кодом из раздела 3.4 плана."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class MediaFormat:
    """Один вариант из списка форматов yt-dlp."""

    format_id: str
    ext: str
    height: int | None = None
    vcodec: str | None = None
    acodec: str | None = None
    filesize: int | None = None
    protocol: str | None = None
    #: Прямая ссылка на CDN. В кэш не попадает: живёт часы и весит килобайты.
    url: str | None = None

    @property
    def has_video(self) -> bool:
        return bool(self.vcodec) and self.vcodec != NO_CODEC

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec) and self.acodec != NO_CODEC

    @property
    def is_unlabelled(self) -> bool:
        """Формат без разметки дорожек.

        Так generic-экстрактор отдаёт одиночный файл: ни высоты, ни кодеков,
        ни размера. Это и есть «неизвестный вебинар» — полноценное видео,
        про которое площадка ничего не сообщила.
        """
        return self.vcodec is None and self.acodec is None

    @property
    def is_progressive(self) -> bool:
        """Видео и звук уже в одном контейнере — ремукс не нужен (раздел 1.5)."""
        return self.is_unlabelled or (self.has_video and self.has_audio)

    @property
    def is_direct_http(self) -> bool:
        """Ссылку можно отдать браузеру: это файл, а не манифест."""
        return self.protocol in DIRECT_PROTOCOLS


@dataclass(frozen=True, slots=True)
class MediaInfo:
    title: str
    extractor: str
    duration: float | None = None
    filesize_approx: int | None = None
    formats: tuple[MediaFormat, ...] = ()

    def without_urls(self) -> MediaInfo:
        """Копия без ссылок на CDN — то, что можно класть в кэш."""
        return replace(
            self,
            formats=tuple(replace(fmt, url=None) for fmt in self.formats),
        )


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


@dataclass(frozen=True, slots=True)
class PostProcess:
    status: str
    processor: str

    @property
    def is_merge_started(self) -> bool:
        return self.status == "started" and self.processor == MERGER


#: Имя постпроцессора, который сшивает раздельные дорожки DASH.
MERGER = "Merger"

ProgressCallback = Callable[[Progress], Awaitable[None]]
PostProcessCallback = Callable[[PostProcess], Awaitable[None]]
#: Получает сам процесс, а не только PID: по номеру из другого контейнера
#: сигнал не отправить, а прерывание задач держится на этом (раздел 2.5.3).
StartCallback = Callable[["asyncio.subprocess.Process"], Awaitable[None]]


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


def parse_postprocess_line(line: str) -> PostProcess | None:
    if not line.startswith(POST_PREFIX):
        return None

    fields = line[len(POST_PREFIX) :].split("|")
    if len(fields) != 2:
        log.debug("не разобрана строка постобработки: %r", line)
        return None

    return PostProcess(status=fields[0], processor=fields[1])


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
        formats=tuple(_parse_format(raw) for raw in info.get("formats") or ()),
    )


def _parse_format(raw: dict[str, object]) -> MediaFormat:
    return MediaFormat(
        format_id=str(raw.get("format_id") or ""),
        ext=str(raw.get("ext") or ""),
        height=_coerce_int(raw.get("height")),
        vcodec=_coerce_str(raw.get("vcodec")),
        acodec=_coerce_str(raw.get("acodec")),
        # filesize известен не всегда: у DASH-фрагментов есть только оценка.
        filesize=_coerce_int(raw.get("filesize") or raw.get("filesize_approx")),
        protocol=_coerce_str(raw.get("protocol")),
        url=_coerce_str(raw.get("url")),
    )


def _coerce_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _coerce_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


async def download(
    url: str,
    *,
    format_spec: str,
    format_sort: str,
    dest_dir: Path,
    timeout: float,
    on_start: StartCallback | None = None,
    on_progress: ProgressCallback | None = None,
    on_postprocess: PostProcessCallback | None = None,
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
        "--progress-template",
        _POSTPROCESS_TEMPLATE,
        "--print",
        _FILEPATH_TEMPLATE,
        "--merge-output-format",
        "mp4",
        "-f",
        format_spec,
        "--format-sort",
        format_sort,
        "-o",
        str(dest_dir / _OUTPUT_TEMPLATE),
        url,
    ]

    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    log.info("yt-dlp запущен, pid=%s, формат=%s", process.pid, format_spec)
    if on_start is not None:
        await on_start(process)

    printed_paths: list[str] = []
    try:
        async with asyncio.timeout(timeout):
            _, stderr_bytes, code = await asyncio.gather(
                _consume_stdout(process, printed_paths, on_progress, on_postprocess),
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


async def terminate_process(
    process: asyncio.subprocess.Process, *, grace: float | None = None
) -> None:
    """Мягкая остановка по процедуре из раздела 2.5.3: SIGTERM, затем SIGKILL."""
    if process.returncode is not None:
        return

    log.info("останавливаю процесс %s", process.pid)
    process.terminate()
    try:
        async with asyncio.timeout(grace if grace is not None else TERMINATE_GRACE_SECONDS):
            await process.wait()
    except TimeoutError:
        log.warning("процесс %s не ответил на SIGTERM, отправляю SIGKILL", process.pid)
        process.kill()
        await process.wait()


async def _consume_stdout(
    process: asyncio.subprocess.Process,
    printed_paths: list[str],
    on_progress: ProgressCallback | None,
    on_postprocess: PostProcessCallback | None,
) -> None:
    async for raw_line in process.stdout:
        line = raw_line.decode(errors="replace").strip()
        if line.startswith(FILE_PREFIX):
            printed_paths.append(line[len(FILE_PREFIX) :])
        elif on_progress is not None and (progress := parse_progress_line(line)):
            await on_progress(progress)
        elif on_postprocess is not None and (stage := parse_postprocess_line(line)):
            await on_postprocess(stage)


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

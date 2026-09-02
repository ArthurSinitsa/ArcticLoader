"""Профили качества — раздел 2.7 плана.

Наружу отдаётся фиксированный набор профилей, а не селекторы yt-dlp: их
синтаксис пользователю не показать, а в API это была бы дыра — произвольное
выражение в `-f` от кого угодно.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from app.services.ytdlp import MediaFormat

log = logging.getLogger(__name__)


class Quality(StrEnum):
    AUDIO = "audio"
    P360 = "360p"
    P480 = "480p"
    P720 = "720p"
    P1080 = "1080p"
    MAX = "max"


#: Умолчание — 1080p, а не «максимум»: 4K-ролик стоит гигабайта трафика в обе
#: стороны вместо трёхсот мегабайт, а разницы на телефоне не видно.
DEFAULT_QUALITY = Quality.P1080

#: Потолок высоты; None — без ограничения.
HEIGHT_CAPS: dict[Quality, int | None] = {
    Quality.P360: 360,
    Quality.P480: 480,
    Quality.P720: 720,
    Quality.P1080: 1080,
    Quality.MAX: None,
}

#: Разрешение важнее кодека, но при равном разрешении H.264 предпочтительнее
#: AV1 и VP9: весит больше, зато открывается везде, включая телевизоры.
FORMAT_SORT = "res,vcodec:h264,acodec:m4a"

AUDIO_SELECTOR = "ba/b"
MAX_SELECTOR = "bv*+ba/b"
#: Последний рубеж: «что есть, то и бери».
FALLBACK_SELECTOR = "b"

SOURCE_QUALITY_LABEL = "Исходное качество"


@dataclass(frozen=True, slots=True)
class QualityOption:
    """Строка выпадающего списка."""

    quality: Quality
    label: str
    height: int | None
    filesize: int | None


def selector(quality: Quality) -> str:
    """Селектор yt-dlp для профиля."""
    if quality is Quality.AUDIO:
        return AUDIO_SELECTOR

    cap = HEIGHT_CAPS[quality]
    if cap is None:
        return MAX_SELECTOR
    # Хвостовой `/b` обязателен: generic-экстрактор отдаёт одиночный файл без
    # высоты, и фильтр [height<=N] его не выбирает — yt-dlp падает с
    # «Requested format is not available».
    return f"bv*[height<={cap}]+ba/b[height<={cap}]/{FALLBACK_SELECTOR}"


def available(formats: tuple[MediaFormat, ...]) -> list[QualityOption]:
    """Профили, которые у этого ролика реально есть.

    Показывать 1080p там, где его нет, — врать пользователю: он выберет и
    молча получит 720p.
    """
    video = [fmt for fmt in formats if fmt.has_video and fmt.height]
    options: list[QualityOption] = []

    if any(fmt.has_audio for fmt in formats):
        options.append(
            QualityOption(
                quality=Quality.AUDIO,
                label="Только звук",
                height=None,
                filesize=_size_of(_best_audio(formats)),
            )
        )

    if not video:
        # Generic-экстрактор ничего не знает про одиночный файл: ни высоты, ни
        # кодеков. Выбирать не из чего, но скачать можно — и это основной
        # сценарий «неизвестного вебинара», ради которого сервис затевался.
        source = max(
            (fmt for fmt in formats if fmt.is_progressive), key=_size_of_or_zero, default=None
        )
        if source is not None:
            options.append(
                QualityOption(
                    quality=Quality.MAX,
                    label=SOURCE_QUALITY_LABEL,
                    height=source.height,
                    filesize=source.filesize,
                )
            )
        return options

    best_height = max(fmt.height for fmt in video)
    seen_heights: set[int] = set()

    for quality, cap in HEIGHT_CAPS.items():
        if quality is Quality.MAX:
            continue
        height = _best_height_under(video, cap)
        # Профиль без своей высоты дублирует предыдущий: если максимум 720p,
        # строки «1080p» в списке быть не должно.
        if height is None or height in seen_heights:
            continue
        seen_heights.add(height)
        options.append(
            QualityOption(
                quality=quality,
                label=f"{height}p",
                height=height,
                filesize=estimate_size(formats, quality),
            )
        )

    if best_height not in seen_heights:
        options.append(
            QualityOption(
                quality=Quality.MAX,
                label=f"Максимум ({best_height}p)",
                height=best_height,
                filesize=estimate_size(formats, Quality.MAX),
            )
        )

    return options


def pick_progressive(formats: tuple[MediaFormat, ...], quality: Quality) -> MediaFormat | None:
    """Progressive-формат, не теряющий качества относительно профиля.

    Главная оптимизация раздела 1.5: если видео и звук уже в одном контейнере,
    сервер в передаче не участвует — клиент качает с CDN сам. Но подменять
    запрошенное качество более низким нельзя, поэтому берём только формат
    с той же высотой, что дал бы ремукс.
    """
    if quality is Quality.AUDIO:
        return None

    cap = HEIGHT_CAPS[quality]
    video = [fmt for fmt in formats if fmt.has_video and fmt.height]
    target = _best_height_under(video, cap)

    candidates = [
        fmt
        for fmt in formats
        # Манифест HLS/DASH браузеру отдавать нельзя: это не файл.
        if fmt.is_progressive
        and fmt.url
        and fmt.is_direct_http
        # У неразмеченного файла высоты нет — сравнивать не с чем и не нужно.
        and (fmt.height == target if target is not None else fmt.is_unlabelled)
    ]
    if not candidates:
        return None

    chosen = max(candidates, key=_codec_then_size)
    log.info(
        "progressive-формат %s (%sp) — отдаём прямой ссылкой, канал сервера свободен",
        chosen.format_id,
        chosen.height,
    )
    return chosen


def estimate_size(formats: tuple[MediaFormat, ...], quality: Quality) -> int | None:
    """Приблизительный размер итогового файла."""
    if quality is Quality.AUDIO:
        return _size_of(_best_audio(formats))

    video = _best_video(formats, HEIGHT_CAPS[quality])
    if video is None or video.filesize is None:
        return None
    if video.is_progressive:
        return video.filesize
    return video.filesize + (_size_of(_best_audio(formats)) or 0)


def _best_height_under(video: list[MediaFormat], cap: int | None) -> int | None:
    heights = [fmt.height for fmt in video if cap is None or fmt.height <= cap]
    return max(heights) if heights else None


def _best_video(formats: tuple[MediaFormat, ...], cap: int | None) -> MediaFormat | None:
    video = [fmt for fmt in formats if fmt.has_video and fmt.height]
    target = _best_height_under(video, cap)
    if target is None:
        return None
    return max((fmt for fmt in video if fmt.height == target), key=_codec_then_size, default=None)


def _best_audio(formats: tuple[MediaFormat, ...]) -> MediaFormat | None:
    audio_only = [fmt for fmt in formats if fmt.has_audio and not fmt.has_video]
    return max(audio_only, key=_size_of_or_zero, default=None)


def _codec_then_size(fmt: MediaFormat) -> tuple[int, int]:
    """Тот же приоритет, что и в FORMAT_SORT: сначала H.264, потом размер."""
    is_h264 = bool(fmt.vcodec and fmt.vcodec.startswith(("avc", "h264")))
    return (int(is_h264), fmt.filesize or 0)


def _size_of(fmt: MediaFormat | None) -> int | None:
    return fmt.filesize if fmt else None


def _size_of_or_zero(fmt: MediaFormat) -> int:
    return fmt.filesize or 0

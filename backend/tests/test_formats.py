import pytest

from app.services.formats import (
    DEFAULT_QUALITY,
    FORMAT_SORT,
    Quality,
    available,
    estimate_size,
    pick_progressive,
    selector,
)
from app.services.ytdlp import MediaFormat

MEGABYTE = 1024 * 1024


def video(height: int, *, vcodec: str = "avc1.64", size: int = 10 * MEGABYTE) -> MediaFormat:
    return MediaFormat(
        format_id=f"v{height}",
        ext="mp4",
        height=height,
        vcodec=vcodec,
        acodec="none",
        filesize=size,
    )


def audio(*, size: int = MEGABYTE) -> MediaFormat:
    return MediaFormat(format_id="a1", ext="m4a", vcodec="none", acodec="mp4a.40.2", filesize=size)


def progressive(
    height: int, *, size: int = 12 * MEGABYTE, url: str | None = "https://cdn/v"
) -> MediaFormat:
    return MediaFormat(
        format_id=f"p{height}",
        ext="mp4",
        height=height,
        vcodec="avc1.64",
        acodec="mp4a.40.2",
        filesize=size,
        protocol="https",
        url=url,
    )


def test_default_quality_is_not_maximum() -> None:
    """Умолчание «максимум» — самая дорогая настройка сервиса (раздел 2.7)."""
    assert DEFAULT_QUALITY is Quality.P1080


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        (Quality.AUDIO, "ba/b"),
        (Quality.P360, "bv*[height<=360]+ba/b[height<=360]/b"),
        (Quality.P1080, "bv*[height<=1080]+ba/b[height<=1080]/b"),
        (Quality.MAX, "bv*+ba/b"),
    ],
)
def test_selector(quality: Quality, expected: str) -> None:
    assert selector(quality) == expected


def test_format_sort_prefers_resolution_then_h264() -> None:
    """Кодек — только тай-брейк: иначе h264 720p победит AV1 1080p."""
    assert FORMAT_SORT.startswith("res,")
    assert "vcodec:h264" in FORMAT_SORT


def test_available_lists_only_real_heights() -> None:
    formats = (video(360), video(720), audio())

    options = available(formats)
    qualities = [option.quality for option in options]

    assert Quality.P360 in qualities
    assert Quality.P720 in qualities
    # 1080p у ролика нет — предлагать его нельзя.
    assert Quality.P1080 not in qualities
    assert Quality.AUDIO in qualities


def test_available_collapses_duplicate_heights() -> None:
    """Максимум 480p: профили 720p и 1080p дали бы ту же картинку."""
    formats = (video(240), video(480), audio())

    labels = [option.label for option in available(formats)]

    assert labels.count("480p") == 1
    assert "720p" not in labels


def test_available_adds_max_only_above_1080() -> None:
    formats = (video(1080), video(2160), audio())

    options = {option.quality: option for option in available(formats)}

    assert options[Quality.MAX].height == 2160
    assert options[Quality.MAX].label == "Максимум (2160p)"


def test_available_without_video_offers_audio_only() -> None:
    options = available((audio(),))

    assert [option.quality for option in options] == [Quality.AUDIO]


def test_available_returns_nothing_for_empty_formats() -> None:
    assert available(()) == []


def test_estimate_size_sums_video_and_audio() -> None:
    formats = (video(720, size=50 * MEGABYTE), audio(size=5 * MEGABYTE))

    assert estimate_size(formats, Quality.P720) == 55 * MEGABYTE


def test_estimate_size_of_progressive_is_not_doubled() -> None:
    formats = (progressive(360, size=20 * MEGABYTE), audio(size=5 * MEGABYTE))

    assert estimate_size(formats, Quality.P360) == 20 * MEGABYTE


def test_estimate_size_is_none_without_data() -> None:
    formats = (video(720, size=None), audio())

    assert estimate_size(formats, Quality.P720) is None


def unlabelled(*, size: int | None = None, protocol: str = "https") -> MediaFormat:
    """Одиночный файл глазами generic-экстрактора: разметки нет вообще."""
    return MediaFormat(
        format_id="mp4",
        ext="mp4",
        height=None,
        vcodec=None,
        acodec=None,
        filesize=size,
        protocol=protocol,
        url="https://coach.example.com/webinar.mp4",
    )


@pytest.mark.parametrize("quality", [Quality.P360, Quality.P720, Quality.P1080])
def test_capped_selectors_fall_back_to_best(quality: Quality) -> None:
    """Без хвостового `/b` yt-dlp падает на файлах без метаданных высоты."""
    assert selector(quality).endswith("/b")


def test_unlabelled_source_is_offered_as_is() -> None:
    """Неизвестный вебинар: выбирать не из чего, но скачать обязаны."""
    options = available((unlabelled(size=5 * MEGABYTE),))

    assert len(options) == 1
    assert options[0].quality is Quality.MAX
    assert options[0].label == "Исходное качество"
    assert options[0].filesize == 5 * MEGABYTE


def test_unlabelled_source_goes_straight_to_the_client() -> None:
    chosen = pick_progressive((unlabelled(),), Quality.P720)

    assert chosen is not None
    assert chosen.url == "https://coach.example.com/webinar.mp4"


def test_hls_manifest_is_never_handed_to_the_browser() -> None:
    """Манифест — не файл: браузер скачает текстовый плейлист вместо видео."""
    manifest = unlabelled(protocol="m3u8_native")

    assert pick_progressive((manifest,), Quality.P720) is None


def test_pick_progressive_requires_http_protocol() -> None:
    dash = MediaFormat(
        format_id="d",
        ext="mp4",
        height=360,
        vcodec="avc1",
        acodec="mp4a",
        protocol="m3u8_native",
        url="https://cdn/playlist.m3u8",
    )

    assert pick_progressive((dash,), Quality.P360) is None


def test_pick_progressive_returns_format_when_quality_is_kept() -> None:
    formats = (progressive(360), video(360), audio())

    chosen = pick_progressive(formats, Quality.P360)

    assert chosen is not None
    assert chosen.format_id == "p360"


def test_pick_progressive_refuses_to_downgrade_quality() -> None:
    """Progressive есть, но только 360p — отдать его вместо 1080p нельзя."""
    formats = (progressive(360), video(1080), audio())

    assert pick_progressive(formats, Quality.P1080) is None


def test_pick_progressive_ignores_formats_without_url() -> None:
    formats = (progressive(360, url=None), audio())

    assert pick_progressive(formats, Quality.P360) is None


def test_pick_progressive_skips_audio_profile() -> None:
    formats = (progressive(360), audio())

    assert pick_progressive(formats, Quality.AUDIO) is None


def test_pick_progressive_prefers_h264_over_av1() -> None:
    av1 = MediaFormat(
        format_id="av1",
        ext="mp4",
        height=360,
        vcodec="av01.0.05M.08",
        acodec="opus",
        filesize=99 * MEGABYTE,
        protocol="https",
        url="https://cdn/av1",
    )
    formats = (av1, progressive(360))

    assert pick_progressive(formats, Quality.P360).format_id == "p360"

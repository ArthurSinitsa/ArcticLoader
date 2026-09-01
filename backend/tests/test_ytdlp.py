import pytest

from app.services import ytdlp


def progress_line(*fields: str) -> str:
    return ytdlp.PROGRESS_PREFIX + "|".join(fields)


def test_parse_progress_line_reads_all_fields() -> None:
    line = progress_line("downloading", "512", "1024", "NA", "128.5", "7")

    progress = ytdlp.parse_progress_line(line)

    assert progress == ytdlp.Progress(
        status="downloading",
        downloaded_bytes=512,
        total_bytes=1024,
        speed=128.5,
        eta=7,
    )
    assert progress.percent == 50.0


def test_parse_progress_line_falls_back_to_estimate() -> None:
    """У DASH-фрагментов точного размера нет — только оценка."""
    line = progress_line("downloading", "250", "NA", "1000", "NA", "NA")

    progress = ytdlp.parse_progress_line(line)

    assert progress.total_bytes == 1000
    assert progress.speed is None
    assert progress.percent == 25.0


def test_parse_progress_line_handles_float_byte_counts() -> None:
    progress = ytdlp.parse_progress_line(
        progress_line("downloading", "512.0", "1024.0", "NA", "NA", "NA")
    )

    assert progress.downloaded_bytes == 512


@pytest.mark.parametrize(
    "line",
    [
        "[download] 50% of 10MiB",
        "",
        progress_line("downloading", "1", "2"),
        ytdlp.FILE_PREFIX + "/srv/media/x.mp4",
    ],
)
def test_parse_progress_line_ignores_foreign_output(line: str) -> None:
    assert ytdlp.parse_progress_line(line) is None


def test_percent_is_none_without_total() -> None:
    progress = ytdlp.Progress(status="downloading", downloaded_bytes=100)

    assert progress.percent is None


def test_percent_is_capped_at_100() -> None:
    progress = ytdlp.Progress(status="downloading", downloaded_bytes=150, total_bytes=100)

    assert progress.percent == 100.0


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("ERROR: Unsupported URL: https://example.com/x", "unsupported_site"),
        ("ERROR: no suitable InfoExtractor for URL", "unsupported_site"),
        ("ERROR: unable to write data: No space left on device", "disk_full"),
        ("ERROR: HTTP Error 429: Too Many Requests", "rate_limited"),
        # Формулировка YouTube про бота — это проблема IP, а не доступа.
        ("ERROR: Sign in to confirm you're not a bot", "rate_limited"),
        ("ERROR: The uploader has not made this video available in your country", "geo_blocked"),
        ("ERROR: Private video. Sign in if you've been granted access", "login_required"),
        ("ERROR: Unable to download webpage: <urlopen error timed out>", "network"),
        ("ERROR: что-то пошло не так", "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_error(stderr: str, expected: str) -> None:
    assert ytdlp.classify_error(stderr) == expected


def test_ytdlp_error_carries_code() -> None:
    error = ytdlp.YtDlpError("geo_blocked", "недоступно в вашей стране")

    assert error.code == "geo_blocked"
    assert str(error) == "недоступно в вашей стране"

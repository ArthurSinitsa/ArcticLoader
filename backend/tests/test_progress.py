"""Подсчёт сквозного прогресса — то, из-за чего полоска бегала дважды."""

from typing import Any

import pytest

from app.services.ytdlp import PostProcess, Progress, parse_postprocess_line
from app.worker.tasks import MAX_DOWNLOAD_PERCENT, _ProgressReporter


class FakeJob:
    def __init__(self) -> None:
        self.reported: list[float] = []

    async def report_progress(self, percent: float) -> None:
        self.reported.append(percent)


def reporter(expected: int | None, **kwargs: Any) -> tuple[_ProgressReporter, FakeJob]:
    job = FakeJob()
    # Интервал нулевой: троттлинг проверяется отдельным тестом.
    return _ProgressReporter(job=job, expected_bytes=expected, interval=0.0, **kwargs), job


def downloading(done: int, total: int | None = None) -> Progress:
    return Progress(status="downloading", downloaded_bytes=done, total_bytes=total)


def finished(total: int) -> Progress:
    return Progress(status="finished", downloaded_bytes=total, total_bytes=total)


async def test_second_stream_continues_instead_of_restarting() -> None:
    """DASH качает видео и звук раздельно; общий процент не должен сбрасываться."""
    reader, job = reporter(100)

    await reader.on_progress(downloading(40, 80))
    await reader.on_progress(finished(80))
    # Пошла аудиодорожка — её собственные байты начинаются с нуля.
    await reader.on_progress(downloading(10, 20))

    assert job.reported == [40.0, 90.0]


async def test_progress_never_goes_backwards() -> None:
    """Оценка размера неточна; полоска, идущая вспять, выглядит поломкой."""
    reader, job = reporter(100)

    await reader.on_progress(downloading(50, 100))
    await reader.on_progress(downloading(30, 100))

    assert job.reported == [50.0, 50.0]


async def test_downloading_never_reaches_hundred() -> None:
    """Сто процентов принадлежат `ready`: после скачивания ещё ремукс."""
    reader, job = reporter(100)

    await reader.on_progress(downloading(100, 100))

    assert job.reported == [MAX_DOWNLOAD_PERCENT]


async def test_falls_back_to_stream_total_without_estimate() -> None:
    reader, job = reporter(None)

    await reader.on_progress(downloading(25, 100))

    assert job.reported == [25.0]


async def test_finished_event_is_not_reported_as_progress() -> None:
    reader, job = reporter(100)

    await reader.on_progress(finished(100))

    assert job.reported == []


async def test_progress_without_totals_is_ignored() -> None:
    reader, job = reporter(None)

    await reader.on_progress(downloading(10, None))

    assert job.reported == []


async def test_writes_are_throttled() -> None:
    """yt-dlp шлёт десятки событий в секунду — в БД и Redis они не нужны."""
    job = FakeJob()
    reader = _ProgressReporter(job=job, expected_bytes=100, interval=60.0)

    await reader.on_progress(downloading(10, 100))
    await reader.on_progress(downloading(20, 100))
    await reader.on_progress(downloading(30, 100))

    assert job.reported == [10.0]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("#ARCTIC_POST#started|Merger", PostProcess(status="started", processor="Merger")),
        ("#ARCTIC_POST#finished|MoveFiles", PostProcess(status="finished", processor="MoveFiles")),
        ("[Merger] Merging formats", None),
        ("#ARCTIC_POST#broken", None),
    ],
)
def test_parse_postprocess_line(line: str, expected: PostProcess | None) -> None:
    assert parse_postprocess_line(line) == expected


def test_only_merger_start_means_remux() -> None:
    assert PostProcess(status="started", processor="Merger").is_merge_started
    assert not PostProcess(status="finished", processor="Merger").is_merge_started
    assert not PostProcess(status="started", processor="MoveFiles").is_merge_started

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import UrlMeta
from app.services import url_meta
from app.services.ytdlp import MediaFormat, MediaInfo, YtDlpError

URL = "https://example.com/watch?v=1"

INFO = MediaInfo(
    title="Видео",
    extractor="Generic",
    duration=42.0,
    formats=(
        MediaFormat(
            format_id="18",
            ext="mp4",
            height=360,
            vcodec="avc1.42",
            acodec="mp4a.40.2",
            filesize=1024,
            url="https://cdn.example.com/secret-signed-url",
        ),
    ),
)


def stub_probe(monkeypatch: pytest.MonkeyPatch, info: MediaInfo | Exception) -> list[str]:
    """Подменяет обращение к площадке и считает вызовы."""
    calls: list[str] = []

    async def probe(url: str, *, timeout: float) -> MediaInfo:
        calls.append(url)
        if isinstance(info, Exception):
            raise info
        return info

    monkeypatch.setattr(url_meta, "probe", probe)
    return calls


async def test_second_call_is_served_from_cache(
    monkeypatch: pytest.MonkeyPatch, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    calls = stub_probe(monkeypatch, INFO)

    async with session_factory() as session:
        first = await url_meta.get_or_fetch(session, URL, ttl_seconds=1800)
        second = await url_meta.get_or_fetch(session, URL, ttl_seconds=1800)

    assert len(calls) == 1
    assert first.title == second.title == "Видео"
    assert second.formats[0].height == 360


async def test_cache_does_not_store_cdn_urls(
    monkeypatch: pytest.MonkeyPatch, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Подписанные ссылки живут часы и весят килобайты — в кэше им не место."""
    stub_probe(monkeypatch, INFO)

    async with session_factory() as session:
        await url_meta.get_or_fetch(session, URL, ttl_seconds=1800)
        row = await session.get(UrlMeta, url_meta.url_hash(URL))
        cached = await url_meta.get_or_fetch(session, URL, ttl_seconds=1800)

    assert "secret-signed-url" not in str(row.payload)
    assert cached.formats[0].url is None


async def test_expired_cache_triggers_new_probe(
    monkeypatch: pytest.MonkeyPatch, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    calls = stub_probe(monkeypatch, INFO)

    async with session_factory() as session:
        await url_meta.get_or_fetch(session, URL, ttl_seconds=1800)
        row = await session.get(UrlMeta, url_meta.url_hash(URL))
        row.fetched_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()

        await url_meta.get_or_fetch(session, URL, ttl_seconds=1800)

    assert len(calls) == 2


async def test_failure_is_not_cached(
    monkeypatch: pytest.MonkeyPatch, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    stub_probe(monkeypatch, YtDlpError("unsupported_site", "Unsupported URL"))

    async with session_factory() as session:
        with pytest.raises(YtDlpError):
            await url_meta.get_or_fetch(session, URL, ttl_seconds=1800)
        row = await session.get(UrlMeta, url_meta.url_hash(URL))

    assert row is None


async def test_purge_expired_removes_only_stale_rows(
    monkeypatch: pytest.MonkeyPatch, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    stub_probe(monkeypatch, INFO)

    async with session_factory() as session:
        await url_meta.get_or_fetch(session, URL, ttl_seconds=1800)
        removed = await url_meta.purge_expired(session, ttl_seconds=1800)
        assert removed == 0

        row = await session.get(UrlMeta, url_meta.url_hash(URL))
        row.fetched_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()

        assert await url_meta.purge_expired(session, ttl_seconds=1800) == 1


def test_url_hash_is_stable_and_distinct() -> None:
    assert url_meta.url_hash(URL) == url_meta.url_hash(URL)
    assert url_meta.url_hash(URL) != url_meta.url_hash(URL + "2")

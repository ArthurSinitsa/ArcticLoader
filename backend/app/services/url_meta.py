"""Кэш метаданных ссылки (раздел 3.1).

Ссылки на CDN в кэш не кладутся: они живут часы, весят по килобайту на формат
и всё равно перевыпускаются при скачивании.
"""

import hashlib
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UrlMeta
from app.services.ytdlp import MediaFormat, MediaInfo, probe

log = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 120.0


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


async def get_or_fetch(session: AsyncSession, url: str, *, ttl_seconds: int) -> MediaInfo:
    """Метаданные из кэша, а если протухли — свежим запросом к площадке."""
    cached = await _load(session, url, ttl_seconds=ttl_seconds)
    if cached is not None:
        log.info("метаданные из кэша: %s", url)
        return cached

    info = await probe(url, timeout=PROBE_TIMEOUT_SECONDS)
    await _store(session, url, info)
    return info


async def _load(session: AsyncSession, url: str, *, ttl_seconds: int) -> MediaInfo | None:
    row = await session.get(UrlMeta, url_hash(url))
    if row is None:
        return None

    if datetime.now(UTC) - _as_utc(row.fetched_at) > timedelta(seconds=ttl_seconds):
        log.debug("кэш метаданных протух: %s", url)
        return None

    return _deserialize(row.payload)


async def _store(session: AsyncSession, url: str, info: MediaInfo) -> None:
    row = UrlMeta(
        url_hash=url_hash(url),
        source_url=url,
        extractor=info.extractor,
        payload=asdict(info.without_urls()),
        fetched_at=datetime.now(UTC),
    )
    try:
        # merge, а не insert: переносимо между Postgres и SQLite, в отличие от
        # диалектного on_conflict_do_update.
        await session.merge(row)
        await session.commit()
    except IntegrityError:
        # Два одновременных запроса на одну ссылку — гонка безобидная,
        # проигравший просто не записал кэш. Ронять из-за этого запрос нельзя.
        await session.rollback()
        log.warning("кэш метаданных не записан из-за гонки: %s", url)


def _as_utc(moment: datetime) -> datetime:
    """SQLite отдаёт наивный datetime даже для timezone=True — дотягиваем до UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _deserialize(payload: dict) -> MediaInfo:
    raw_formats = payload.get("formats") or ()
    return MediaInfo(
        title=payload["title"],
        extractor=payload["extractor"],
        duration=payload.get("duration"),
        filesize_approx=payload.get("filesize_approx"),
        formats=tuple(MediaFormat(**raw) for raw in raw_formats),
    )


async def purge_expired(session: AsyncSession, *, ttl_seconds: int) -> int:
    """Чистка протухших записей — вызывается cron-задачей Этапа 9."""
    deadline = datetime.now(UTC) - timedelta(seconds=ttl_seconds)
    result = await session.execute(sa.delete(UrlMeta).where(UrlMeta.fetched_at < deadline))
    await session.commit()
    return result.rowcount

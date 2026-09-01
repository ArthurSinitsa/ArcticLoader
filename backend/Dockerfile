FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg нужен yt-dlp для склейки раздельных видео- и аудиодорожек DASH.
# Это ремукс (-c copy), а не перекодирование — раздел 1.4 плана.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --requirement requirements.txt

COPY . .

# yt-dlp исполняет код экстракторов на произвольных страницах — не от root.
RUN useradd --create-home --uid 1000 arctic \
    && mkdir -p /srv/media \
    && chown -R arctic:arctic /srv/media /app
USER arctic

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM base AS dev

USER root
COPY requirements-dev.txt ./
RUN pip install --requirement requirements-dev.txt
USER arctic

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg нужен yt-dlp для склейки раздельных видео- и аудиодорожек DASH.
# Это ремукс (-c copy), а не перекодирование — раздел 1.4 плана.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/*

# JS-runtime для yt-dlp. Без него YouTube отдаёт усечённый список форматов:
# проверено — потолок падает до 720p и остаётся только AV1 (раздел 3.3).
ARG DENO_VERSION=2.9.6
RUN case "$(dpkg --print-architecture)" in \
        amd64) DENO_TARGET=x86_64-unknown-linux-gnu ;; \
        arm64) DENO_TARGET=aarch64-unknown-linux-gnu ;; \
        *) echo "неподдерживаемая архитектура" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/deno.zip \
        "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${DENO_TARGET}.zip" \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && deno --version

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

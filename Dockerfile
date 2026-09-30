# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

# An audio-only ffmpeg: Debian's pulls in ~450 MB of video and GPU libraries. Echolot needs the audio
# decoders, FLAC, soxr resampling and the chromaprint muxer (the audio check); yt-dlp remuxes, tags and
# may convert to MP3. Same versions as Debian 13 (ffmpeg 7.1.5, chromaprint 1.5.1 with kissfft), so the
# fingerprints and the resampling stay the same. Rebuilt only when this stage changes.
FROM python:3.13-slim AS ffmpeg
ARG FFMPEG_VERSION=7.1.5 \
    FFMPEG_SHA256=de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f \
    CHROMAPRINT_VERSION=1.5.1 \
    CHROMAPRINT_SHA256=a1aad8fa3b8b18b78d3755b3767faff9abb67242e01b478ec9a64e190f335e1c
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake nasm pkg-config curl \
    ca-certificates xz-utils libsoxr-dev libmp3lame-dev libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL -o chromaprint.tar.gz \
    "https://github.com/acoustid/chromaprint/releases/download/v${CHROMAPRINT_VERSION}/chromaprint-${CHROMAPRINT_VERSION}.tar.gz" \
    && echo "${CHROMAPRINT_SHA256}  chromaprint.tar.gz" | sha256sum -c - \
    && tar xzf chromaprint.tar.gz \
    && cmake -S "chromaprint-${CHROMAPRINT_VERSION}" -B chromaprint-build -DCMAKE_BUILD_TYPE=Release \
       -DCMAKE_INSTALL_PREFIX=/usr/local -DBUILD_SHARED_LIBS=OFF -DBUILD_TOOLS=OFF -DBUILD_TESTS=OFF -DFFT_LIB=kissfft \
    && cmake --build chromaprint-build -j "$(nproc)" && cmake --install chromaprint-build
RUN curl -fsSL -o ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
    && echo "${FFMPEG_SHA256}  ffmpeg.tar.xz" | sha256sum -c - \
    && tar xJf ffmpeg.tar.xz && cd "ffmpeg-${FFMPEG_VERSION}" \
    && ./configure --prefix=/usr/local --pkg-config-flags=--static --extra-libs="-lstdc++ -lm" --enable-version3 \
       --disable-debug --disable-doc --disable-ffplay --disable-autodetect --disable-hwaccels --disable-devices \
       --enable-chromaprint --enable-libsoxr --enable-libmp3lame --enable-openssl --enable-zlib --enable-iconv \
    && make -j "$(nproc)" && make install

FROM python:3.13-slim
ARG ECHOLOT_COMMIT=""
LABEL org.opencontainers.image.title="Echolot" \
      org.opencontainers.image.description="Spotify and SoundCloud lists as a local music library, in the best quality there is" \
      org.opencontainers.image.source="https://github.com/Zweisteinium/echolot" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.revision="${ECHOLOT_COMMIT}"
# the audio-only ffmpeg's libraries; nodejs: yt-dlp's JavaScript runtime (YouTube)
RUN apt-get update && apt-get install -y --no-install-recommends libsoxr0 libmp3lame0 nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    ECHOLOT_DATA_DIR=/data \
    ECHOLOT_COMMIT=${ECHOLOT_COMMIT} \
    XDG_CACHE_HOME=/data/.cache \
    ECHOLOT_HOST=0.0.0.0 \
    ECHOLOT_PORT=8490
EXPOSE 8490
USER 1000:1000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"ECHOLOT_PORT\"]}/healthz', timeout=4)"]
CMD ["echolot", "serve"]

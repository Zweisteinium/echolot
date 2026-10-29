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
# fingerprints and the resampling stay the same. Shared libraries: ffmpeg and ffprobe use one copy.
# Rebuilt only when this stage changes.
FROM python:3.13-slim AS ffmpeg
ARG FFMPEG_VERSION=7.1.5 \
    FFMPEG_SHA256=de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f \
    CHROMAPRINT_VERSION=1.5.1 \
    CHROMAPRINT_SHA256=a1aad8fa3b8b18b78d3755b3767faff9abb67242e01b478ec9a64e190f335e1c
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake nasm pkg-config curl \
    ca-certificates xz-utils libsoxr-dev libmp3lame-dev libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL --retry 3 --retry-all-errors --connect-timeout 30 -o chromaprint.tar.gz \
    "https://github.com/acoustid/chromaprint/releases/download/v${CHROMAPRINT_VERSION}/chromaprint-${CHROMAPRINT_VERSION}.tar.gz" \
    && echo "${CHROMAPRINT_SHA256}  chromaprint.tar.gz" | sha256sum -c - \
    && tar xzf chromaprint.tar.gz \
    && cmake -S "chromaprint-${CHROMAPRINT_VERSION}" -B chromaprint-build -DCMAKE_BUILD_TYPE=Release \
       -DCMAKE_INSTALL_PREFIX=/usr/local -DBUILD_SHARED_LIBS=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_TOOLS=OFF -DBUILD_TESTS=OFF -DFFT_LIB=kissfft \
    && cmake --build chromaprint-build -j "$(nproc)" && cmake --install chromaprint-build
# ffmpeg.org drops connections from CI runners at times: retried, then Debian's copy (the same tarball, same checksum)
RUN for url in "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
        "https://deb.debian.org/debian/pool/main/f/ffmpeg/ffmpeg_${FFMPEG_VERSION}.orig.tar.xz"; do \
      curl -fsSL --retry 3 --retry-all-errors --connect-timeout 30 -o ffmpeg.tar.xz "$url" \
      && echo "${FFMPEG_SHA256}  ffmpeg.tar.xz" | sha256sum -c - && break; rm -f ffmpeg.tar.xz; done \
    && test -f ffmpeg.tar.xz && tar xJf ffmpeg.tar.xz && cd "ffmpeg-${FFMPEG_VERSION}" \
    && ./configure --prefix=/usr/local --enable-shared --disable-static --extra-libs="-lstdc++ -lm" --enable-version3 \
       --disable-debug --disable-doc --disable-ffplay --disable-autodetect --disable-hwaccels --disable-devices \
       --enable-chromaprint --enable-libsoxr --enable-libmp3lame --enable-openssl --enable-zlib --enable-iconv \
    && make -j "$(nproc)" && make install

# lossprint (MIT, github.com/maxdjohnson/lossprint): the second lossy check, a CNN on spectrograms (audio.lossprint).
# The release binary, pinned by its checksum; its model is built in.
FROM python:3.13-slim AS lossprint
ARG TARGETARCH LOSSPRINT_VERSION=1.1 \
    LOSSPRINT_SHA256_AMD64=7111af755c74be04400386047afb9e5e7b386d87f6de3f9c906992e34d96eba7 \
    LOSSPRINT_SHA256_ARM64=f362c3e4b6dffb8aa72d436a0e28dad840ae03edb2a418f88169c3b9b6d2db5f
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN if [ "${TARGETARCH:-amd64}" = arm64 ]; then arch=aarch64 sum="$LOSSPRINT_SHA256_ARM64"; \
    else arch=x86_64 sum="$LOSSPRINT_SHA256_AMD64"; fi \
    && curl -fsSL -o lossprint.tar.gz "https://github.com/maxdjohnson/lossprint/releases/download/v${LOSSPRINT_VERSION}/lossprint-v${LOSSPRINT_VERSION}-${arch}-unknown-linux-gnu.tar.gz" \
    && echo "${sum}  lossprint.tar.gz" | sha256sum -c - && tar xzf lossprint.tar.gz \
    && install -D -m 755 "lossprint-${arch}-unknown-linux-gnu/lossprint" /out/bin/lossprint \
    && install -D -m 644 "lossprint-${arch}-unknown-linux-gnu/LICENSE" /out/share/doc/lossprint/LICENSE

FROM python:3.13-slim
ARG ECHOLOT_VERSION="" ECHOLOT_COMMIT=""
LABEL org.opencontainers.image.title="Echolot" \
      org.opencontainers.image.description="Spotify and SoundCloud lists as a local music library, in the best quality there is" \
      org.opencontainers.image.source="https://github.com/Zweisteinium/echolot" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${ECHOLOT_VERSION}" \
      org.opencontainers.image.revision="${ECHOLOT_COMMIT}"
# the audio-only ffmpeg's libraries; QuickJS: yt-dlp's JavaScript runtime for YouTube (Debian's node is
# older than yt-dlp supports); curl: Bandcamp's pages (services/bandcamp)
RUN apt-get update && apt-get install -y --no-install-recommends libsoxr0 libmp3lame0 quickjs ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/
COPY --from=ffmpeg /usr/local/lib/*.so.* /usr/local/lib/
COPY --from=lossprint /out/ /usr/local/
RUN ldconfig
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

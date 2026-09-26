# Sockseek (Soulseek batch downloader) + yt-dlp/ffmpeg for fallbacks and SoundCloud.
# Adapted from upstream src/Dockerfile (fiso64/slsk-batchdl): upstream still installs the .NET 6 SDK
# although the project targets net10.0, so the build stage uses Microsoft's SDK image instead.
# Sockseek source: git clone https://github.com/fiso64/sockseek src (next to this file). Update: git -C src pull, then
# docker compose up -d --build
FROM mcr.microsoft.com/dotnet/sdk:10.0-alpine AS build
WORKDIR /app
COPY src/ /app
RUN dotnet restore /app/Sockseek.HelpGenerator/Sockseek.HelpGenerator.csproj \
    && dotnet restore /app/Sockseek.Cli/Sockseek.Cli.csproj -r linux-musl-x64 \
    && dotnet publish /app/Sockseek.Cli/Sockseek.Cli.csproj -c Release -r linux-musl-x64 \
      -p:PublishSingleFile=true -p:PublishTrimmed=true -p:OpenApiGenerateDocuments=false --self-contained=true -o /out \
    && rm -f /out/*.pdb && ls -la /out

FROM ghcr.io/linuxserver/baseimage-alpine:3.20 AS app
ENV TZ=Europe/Berlin
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
# runtime deps for the self-contained .NET binary + yt-dlp/ffmpeg + script deps
RUN apk --no-cache add libstdc++ libgcc icu-libs openssl zlib ffmpeg python3 py3-pip py3-numpy nodejs \
    && pip install --no-cache-dir --break-system-packages "yt-dlp[default]" mutagen pyyaml \
    && (pip install --no-cache-dir --break-system-packages curl_cffi || echo "curl_cffi unavailable, impersonation disabled") \
    && rm -rf /root/.cache /tmp/*
ENV DOCKER_MODS=linuxserver/mods:universal-cron
COPY src/docker/root/ /
COPY --from=build /out/ /usr/bin/

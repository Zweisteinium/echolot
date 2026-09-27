# The official Sockseek image (built from src/, a clone of github.com/fiso64/sockseek, by the sockseek-upstream
# service in docker-compose.yml) plus what the music-sync pipeline needs on top: yt-dlp and ffmpeg for SoundCloud
# and the YouTube fallback, Python with mutagen, pyyaml and numpy for the scripts.
# Update: git -C src pull --ff-only && docker compose build && docker compose up -d
FROM sockseek-upstream
ENV TZ=Europe/Berlin
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
RUN apk --no-cache add ffmpeg python3 py3-pip py3-numpy nodejs \
    && pip install --no-cache-dir --break-system-packages "yt-dlp[default]" mutagen pyyaml \
    && (pip install --no-cache-dir --break-system-packages curl_cffi || echo "curl_cffi unavailable, impersonation disabled") \
    && rm -rf /root/.cache /tmp/*

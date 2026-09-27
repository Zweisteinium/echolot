#!/bin/sh
# Dispatch cron jobs by container role (ROLE=main runs Soulseek jobs, ROLE=fallback runs the YouTube/SoundCloud fallback).
# Pause all downloading: touch /config/state/PAUSED (remove it to resume); playlist rebuilds keep running.
job="$1"
if [ -f /config/state/PAUSED ] && [ "$job" != playlists ]; then
  echo "$(date '+%F %T') $job skipped: downloads paused (/config/state/PAUSED)" >> "/config/logs/$job.log"; exit 0
fi
case "$ROLE:$job" in
  main:sync|main:sweep|main:upgrade|main:playlists|fallback:soundcloud|fallback:fallback) exec /config/scripts/music-sync.py "$job" >> "/config/logs/$job.log" 2>&1 ;;
  *) exit 0 ;;
esac

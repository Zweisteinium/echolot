#!/bin/sh
# Sockseek on-complete hook (option update-index: whatever this prints on stdout updates the index row).
# Sockseek downloads into /music/inbox/sockseek; this hook checks the file and hands it to library.py,
# the only code that puts files into /music/tracks (exact same-song rule, never overwrites anything).
# $1 = downloaded file (inbox, named by CSV row), $2 = CSV row, $3 = source URI (spotify:track:... or empty),
# $4/$5 = Soulseek file and folder name. Artist, title and length of the wanted song are read from the input CSV
# ($MUSIC_SYNC_CSV, set by music-sync.py), not passed as arguments: Sockseek pastes values into the command unescaped,
# so a " in a title used to cut the arguments short.
# 1) validate: the audio codec must match the extension (fake "FLAC" = MP3 renamed to .flac,
#    or an unreadable file) -> delete it and report failed, so the song stays missing and is retried
# 2) repair FLACs with junk before the header (mutagen cannot read them) by remuxing with ffmpeg
# 3) convert WAV/AIFF/ALAC to FLAC (lossless, smaller, standard tags)
# 4) hi-res (> 48 kHz) -> 44.1/48 kHz, 24 bit (inaudible difference, about half the size)
# 5) spectrum check (spectrum.py): a FLAC re-encoded from MP3/AAC counts as lossy ("fake")
# 6) library.py checks it is really the requested artist + title + length (else kept in inbox/review) and files it: new song -> added; same song already there -> discarded, unless this is a genuine
#    lossless copy of a lossy/fake one, which takes over (the old file goes to inbox/replaced for 30 days)
# 7) embed the Spotify cover / artist image if missing (artwork.py, output to stderr)
# The library path is printed as "success;<path>" so Sockseek's index points at it.
f="$1"; row="$2"; uri="$3"; slskfile="$4"; slskfolder="$5"
stem="${f%.*}"; ext=$(printf '%s' "${f##*.}" | tr 'A-Z' 'a-z')
LOG=/config/logs/post-track.log
note() { echo "$(date "+%F %T") $*" >> "$LOG"; }
[ -f "$f" ] || { note "FAIL missing file: $f"; echo "failed;"; exit 0; }
# first field only: with cover art / side data ffprobe prints e.g. "flac," which once made valid files look fake
probe() { ffprobe -v error -select_streams a:0 -show_entries "stream=$1" -of csv=p=0 "$f" 2>/dev/null | head -1 | cut -d, -f1 | tr -d " \r"; }
flac_ok() { python3 -c "import sys; from mutagen.flac import FLAC; FLAC(sys.argv[1])" "$1" 2>/dev/null; }
codec=$(probe codec_name)
ok=0
case "$ext:$codec" in
  flac:flac|mp3:mp3|m4a:aac|m4a:alac|aac:aac|opus:opus|ogg:vorbis|ogg:opus|wav:pcm_*|aiff:pcm_*|aif:pcm_*|wma:wmav*|ape:ape) ok=1 ;;
esac
if [ "$ok" = 0 ]; then
  note "FAIL codec '$codec' vs .$ext: $f"
  rm -f "$f"; echo "failed;"; exit 0
fi
if [ "$ext" = flac ] && ! flac_ok "$f"; then
  # remux first (bit-identical); if the STREAMINFO block is broken, decode and re-encode (still lossless)
  if { ffmpeg -y -v error -i "$f" -map 0:a:0 -c copy -map_metadata 0 -f flac "$stem.fix.tmp" 2>/dev/null || ffmpeg -y -v error -i "$f" -map 0:a:0 -c:a flac -compression_level 5 -map_metadata 0 -f flac "$stem.fix.tmp" 2>/dev/null; } \
     && flac_ok "$stem.fix.tmp"; then
    mv -f "$stem.fix.tmp" "$f"; echo "post-track: repaired FLAC of $f" >&2
  else
    note "FAIL unreadable FLAC: $f"; rm -f "$stem.fix.tmp" "$f"; echo "failed;"; exit 0
  fi
fi
case "$ext:$codec" in
  wav:*|aiff:*|aif:*|m4a:alac)
    # many WAVs from Soulseek carry an ID3 block in front of the RIFF header that tag tools and players choke on
    if ffmpeg -y -v error -i "$f" -map 0:a:0 -c:a flac -compression_level 5 -map_metadata 0 -f flac "$stem.flac.tmp" 2>/dev/null \
       && flac_ok "$stem.flac.tmp"; then
      mv -f "$stem.flac.tmp" "$stem.flac" && rm -f "$f"; f="$stem.flac"; ext=flac
    else
      rm -f "$stem.flac.tmp"; echo "post-track: FLAC conversion failed, keeping $f" >&2
    fi ;;
esac
fake=""
if [ "$ext" = flac ]; then
  sr=$(probe sample_rate)
  if [ "${sr:-0}" -gt 48000 ]; then
    if [ $((sr % 44100)) = 0 ]; then target=44100; else target=48000; fi
    if ffmpeg -y -v error -i "$f" -map 0:a:0 -af aresample=resampler=soxr:precision=28 -ar $target \
         -c:a flac -sample_fmt s32 -bits_per_raw_sample 24 -compression_level 5 -map_metadata 0 -f flac "$stem.rs.tmp" 2>/dev/null \
       && flac_ok "$stem.rs.tmp"; then
      mv -f "$stem.rs.tmp" "$f"; echo "post-track: resampled $sr Hz -> $target Hz / 24 bit: $f" >&2
    else
      rm -f "$stem.rs.tmp"; echo "post-track: resampling failed, keeping $sr Hz: $f" >&2
    fi
  fi
  [ "$(python3 /config/scripts/spectrum.py "$f" 2>/dev/null | cut -d: -f1)" = lossy ] && fake="--fake"
fi
# the wanted song (artist, title, length, uri) from the input CSV row; the URI must agree when both are known
want=$(python3 -c '
import csv, os, sys
rows = list(csv.DictReader(open(os.environ["MUSIC_SYNC_CSV"], encoding="utf-8", newline="")))
r = rows[int(sys.argv[1]) - 2]                        # Sockseek counts the header as row 1
if sys.argv[2] and r.get("uri") and sys.argv[2] != r["uri"]: sys.exit(1)
print("\t".join([r.get("want_artist") or r["Artist"], r.get("want_title") or r["Title"], r["Length"], r.get("uri", ""),
                 r.get("tries") or "0"]))' "$row" "$uri" 2>>"$LOG") \
  || { note "FAIL no CSV row $row (uri $uri) for $f"; echo "failed;"; exit 0; }
sartist=$(printf '%s' "$want" | cut -f1); stitle=$(printf '%s' "$want" | cut -f2); slength=$(printf '%s' "$want" | cut -f3); uri=$(printf '%s' "$want" | cut -f4); tries=$(printf '%s' "$want" | cut -f5)
# a song Soulseek did not find twice (music-sync.py LOOSEN) may be filed on a probable match, marked for review
relaxed=""; [ "${tries:-0}" -ge 2 ] 2>/dev/null && relaxed="--relaxed"
out=$(python3 /config/scripts/library.py file "$f" --artist "$sartist" --title "$stitle" --length "${slength:-0}" \
        --source soulseek --id "$uri" --strict --loose --file-name "$slskfile" --folder "$slskfolder" $fake $relaxed --tries "${tries:-0}" 2>>"$LOG")
action=$(printf '%s' "$out" | cut -f1); dest=$(printf '%s' "$out" | cut -f2-)
# another version of the song (length off by more than max(10 s, 4 %)): kept in inbox/review, the song stays missing
if [ "$action" = mismatch ]; then note "mismatch (other version, kept for review): $f wanted ${slength}s"; echo "failed;"; exit 0; fi
# not the requested artist/title (tags and Soulseek names checked): kept in inbox/review, the song stays missing
if [ "$action" = wrong-song ]; then note "wrong-song (kept for review): $slskfolder/$slskfile for $sartist - $stitle"; echo "failed;"; exit 0; fi
if [ -z "$dest" ] || [ ! -f "$dest" ]; then note "FAIL filing: $f ($out)"; echo "failed;"; exit 0; fi
if [ "$action" != duplicate ]; then
  [ -n "$fake" ] && python3 /config/scripts/spectrum.py "$dest" --record >/dev/null 2>&1
  if [ -n "$uri" ]; then /usr/bin/python3 /config/scripts/artwork.py "$dest" "$uri" >&2 || true
  else /usr/bin/python3 /config/scripts/artwork.py "$dest" search "" "$sartist" "$stitle" >&2 || true; fi
fi
note "$action${fake:+ (lossy-sourced)}: $dest"
echo "success;$dest"
exit 0

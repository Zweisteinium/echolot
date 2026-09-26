#!/usr/bin/env bash
# Hand out gluetun's ProtonVPN forwarded ports to the two Soulseek clients behind it.
# gluetun requests two ports (VPN_PORT_FORWARDING_PORTS_COUNT=2) and lists them in its port file.
# Each client must listen on the public port number itself, because that is the port it announces
# to the Soulseek server; without an open port, transfers with firewalled peers are impossible.
#   slskd:    keeps its current port while it is still forwarded, else takes the first one;
#             slskd.yml is updated and slskd restarted only when the port changed.
#   Sockseek: gets the other port via /opt/sockseek/config/state/listen-port, which
#             music-sync.py passes as --listen-port at the start of every run.
# Run it from the host's crontab every 10 minutes (paths overridable: SLSKD_CONF, SOCKSEEK_PORT_FILE).
set -euo pipefail
CONF=${SLSKD_CONF:-/opt/slskd/slskd/slskd.yml}
SOCKSEEK_PORT_FILE=${SOCKSEEK_PORT_FILE:-/opt/sockseek/config/state/listen-port}
mapfile -t PORTS < <(docker exec gluetun cat /tmp/gluetun/forwarded_port 2>/dev/null | grep -E '^[0-9]+$' || true)
[ "${#PORTS[@]}" -gt 0 ] || { echo "$(date -Is) no forwarded port from gluetun"; exit 0; }

CUR=$(sed -n 's/^  listen_port: \([0-9]*\).*/\1/p' "$CONF" | head -1)
SLSKD=${PORTS[0]}
for p in "${PORTS[@]}"; do [ "$p" = "$CUR" ] && SLSKD=$CUR; done
SOCKSEEK=""
for p in "${PORTS[@]}"; do [ "$p" != "$SLSKD" ] && { SOCKSEEK=$p; break; }; done

if [ "$SLSKD" != "$CUR" ]; then
  # rewrite in place: the file is david's, but its directory is root-owned (no sed -i temp file)
  NEW=$(sed "s/^  listen_port: [0-9]*/  listen_port: $SLSKD/" "$CONF")
  printf '%s\n' "$NEW" > "$CONF"
  docker restart slskd >/dev/null
  echo "$(date -Is) slskd listen_port $CUR -> $SLSKD (restarted)"
fi

OLD=$(cat "$SOCKSEEK_PORT_FILE" 2>/dev/null || true)
if [ -z "$SOCKSEEK" ]; then
  if [ -n "$OLD" ]; then
    rm -f "$SOCKSEEK_PORT_FILE"
    echo "$(date -Is) only one forwarded port, Sockseek port cleared"
  fi
elif [ "$SOCKSEEK" != "$OLD" ]; then
  echo "$SOCKSEEK" > "$SOCKSEEK_PORT_FILE.tmp" && mv "$SOCKSEEK_PORT_FILE.tmp" "$SOCKSEEK_PORT_FILE"
  echo "$(date -Is) sockseek listen_port ${OLD:-none} -> $SOCKSEEK (applies from its next run)"
fi

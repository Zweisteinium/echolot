#!/bin/sh
# The Sockseek daemon for Echolot, in the VPN's network namespace. It is restarted when its login
# (daemon.conf, written by Echolot's Accounts page) or its listen port (listen-port: the VPN's forwarded
# port, written by host/sync-listen-port.sh) changes; the daemon reads both only at its start.
conf=/daemon/daemon.conf
portfile=/daemon/listen-port
pid=""
trap '[ -n "$pid" ] && kill "$pid" 2>/dev/null; exit 0' TERM INT
state() { cat "$conf" "$portfile" 2>/dev/null | cksum; }
while :; do
  if [ ! -s "$conf" ]; then sleep 15; continue; fi  # no account yet
  set -- daemon -c "$conf" --server-ip 0.0.0.0 --server-port 5031 -o /music/inbox/soulseek
  port=$(tr -dc 0-9 < "$portfile" 2>/dev/null)
  [ -n "$port" ] && set -- "$@" --listen-port "$port"
  seen=$(state)
  sockseek "$@" &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 15
    if [ "$(state)" != "$seen" ]; then
      echo "run.sh: login or listen port changed, restarting the daemon"
      kill "$pid"; wait "$pid"
      break
    fi
  done
  pid=""
  sleep 5
done

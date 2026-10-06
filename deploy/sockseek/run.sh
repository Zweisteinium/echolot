#!/bin/sh
# Runs the Sockseek daemon for Echolot and restarts it when its login (daemon.conf, written by Echolot's
# Accounts page) or its listen port (listen-port: a VPN's forwarded port, written by gluetun in vpn.yaml)
# changes; the daemon reads both only at its start. Without a login it waits.
# Watchdog: a daemon that stays disconnected from Soulseek for 15 minutes is restarted. After a server
# outage Sockseek's reconnect can fail for good: its old listener still holds the listen port, so every
# login fails with "Failed to start listening". A fresh daemon reports state None (it logs in with the
# next search), so an idle one is never restarted.
conf=/daemon/daemon.conf
portfile=/daemon/listen-port
status=http://127.0.0.1:5031/api/server/status
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
  down=0  # seconds disconnected from Soulseek
  while kill -0 "$pid" 2>/dev/null; do
    sleep 15
    if [ "$(state)" != "$seen" ]; then
      echo "run.sh: login or listen port changed, restarting the daemon"
      kill "$pid"; wait "$pid"
      break
    fi
    if wget -qO- -T 10 "$status" 2>/dev/null | grep -q '"Disconnected"'; then down=$((down + 15)); else down=0; fi
    if [ "$down" -ge 900 ]; then
      echo "run.sh: disconnected from Soulseek for 15 min, restarting the daemon"
      kill "$pid"; wait "$pid"
      break
    fi
  done
  pid=""
  sleep 5
done

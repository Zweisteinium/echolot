#!/bin/sh
# Deploy an Echolot instance on this server: <instance dir> holds docker-compose.yml (service "echolot"),
# src/ (a clone of the GitHub repository) and data/.
#
#   tools/deploy.sh <instance dir> [<branch>]   the newest commit of the branch (default: dev)
#   tools/deploy.sh <instance dir> --rollback   the image that ran before the last deploy
#
# ONLY_BRANCH=main (production's wrapper) refuses any other branch and takes main fast-forward only.
# Steps: fetch and check out the branch (a clone with changes of its own is refused), build the image while
# the jobs go on (tagged <image>-<commit> as well), pause the jobs and wait for the songs in progress (at
# most WAIT_MINUTES, default 60), back up the database into data/backups/ when the schema version changes
# (the last 10 kept), start, wait until healthy and /healthz reports the commit, resume the jobs if they ran.
set -eu

if [ -z "${DEPLOY_COPY:-}" ]; then  # the pull below may change this file: run a copy of it
    copy=$(mktemp)
    cp "$0" "$copy"
    DEPLOY_COPY=$copy exec sh "$copy" "$@"
fi
trap 'rm -f "$DEPLOY_COPY"' EXIT

[ $# -ge 1 ] || { echo "usage: $0 <instance dir> [<branch> | --rollback]" >&2; exit 2; }
cd "$1"
name=$(basename "$(pwd)")
target=${2:-dev}
container=$(docker compose ps -a -q echolot 2>/dev/null || true)
image=$(docker compose config --images echolot)
mkdir -p data/backups

in_echolot() { docker compose exec -T echolot "$@"; }
schema_now() { in_echolot python -c "import sqlite3; print(sqlite3.connect('file:/data/echolot.db?mode=ro', uri=True).execute('pragma user_version').fetchone()[0])"; }

healthy() {  # wait until the container is healthy and reports the commit
    for _ in $(seq 60); do
        id=$(docker compose ps -q echolot)
        if [ -n "$id" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' "$id")" = healthy ]; then
            got=$(in_echolot python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8490/healthz'))['commit'])")
            [ "$got" = "$1" ] && return 0
            echo "healthz reports $got, not $1" >&2
            return 1
        fi
        sleep 2
    done
    echo "not healthy after 120 s: docker compose logs echolot" >&2
    return 1
}

if [ "$target" = --rollback ]; then
    [ -s data/deploy.previous ] || { echo "nothing to roll back to (data/deploy.previous)" >&2; exit 1; }
    read -r prev_id prev_rev prev_schema < data/deploy.previous
    docker tag "$prev_id" "$image"
    docker compose up -d
    echo "$name rolled back to $prev_rev (schema $prev_schema; a newer database: see data/backups/)"
    exit 0
fi

if [ -n "${ONLY_BRANCH:-}" ] && [ "$target" != "$ONLY_BRANCH" ]; then
    echo "$name deploys only $ONLY_BRANCH, not $target" >&2
    exit 2
fi
if [ -n "$(git -C src status --porcelain)" ]; then
    echo "src/ has changes of its own: commit or discard them first" >&2
    exit 1
fi
was=?  # the commit that runs now (the image's, as /healthz reports it)
if [ -n "$container" ]; then
    was=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container" | sed -n 's/^ECHOLOT_COMMIT=//p')
fi
git -C src fetch -q origin
if [ -n "${ONLY_BRANCH:-}" ]; then
    git -C src checkout -q "$target"
    git -C src merge -q --ff-only "origin/$target"
else
    git -C src checkout -q --detach "origin/$target"
fi
rev=$(git -C src rev-parse --short HEAD)
version=$(sed -n 's/^version = "\(.*\)"$/\1/p' src/pyproject.toml)
schema_new=$(sed -n 's/^VERSION = \([0-9]*\)$/\1/p' src/src/echolot/db.py)

docker compose build -q --build-arg ECHOLOT_VERSION="$version" --build-arg ECHOLOT_COMMIT="$rev" echolot
docker tag "$image" "$image-$rev"

paused=yes
schema_old=
if [ -n "$(docker compose ps -q --status running echolot)" ]; then
    paused=$(in_echolot echolot jobs status | sed -n 's/^paused: //p')
    if ! in_echolot echolot jobs pause --wait --timeout "${WAIT_MINUTES:-60}"; then
        if [ "$paused" = no ]; then in_echolot echolot jobs resume; fi
        echo "$name $rev built, not deployed: jobs still running (WAIT_MINUTES=120 waits longer)" >&2
        exit 1
    fi
    schema_old=$(schema_now)
    if [ "$schema_old" != "$schema_new" ]; then
        backup=echolot-$(date +%Y%m%d-%H%M%S)-v$schema_old.db
        in_echolot python -c "import sqlite3; sqlite3.connect('/data/echolot.db').execute(\"VACUUM INTO '/data/backups/$backup'\")"
        echo "database backed up: data/backups/$backup (schema $schema_old, the new code has $schema_new)"
        ls -1t data/backups/echolot-*.db | tail -n +11 | while read -r old; do rm -f "$old"; done
    fi
fi
if [ -n "$container" ]; then  # what runs now, for --rollback
    echo "$(docker inspect -f '{{.Image}}' "$container") $was ${schema_old:-?}" > data/deploy.previous
fi

docker compose up -d
if ! healthy "$rev"; then
    echo "$name $rev started but not healthy; the jobs stay paused. Back: $0 $1 --rollback" >&2
    exit 1
fi
if [ "$paused" = no ]; then in_echolot echolot jobs resume; fi
echo "$name $rev deployed ($target)"

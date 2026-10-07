"""Command line: `echolot serve`, users, configuration file, secrets, jobs, tags, `echolot version`."""

import argparse
import getpass
import sys
from pathlib import Path

from echolot import COMMIT, __version__
from echolot.config import Settings


def _user(args: argparse.Namespace, settings: Settings) -> int:
    """The users (Navidrome's accounts that logged in), and their rights: the way back in when no admin is
    left, since there is no password here to reset."""
    from echolot import db
    from echolot.settings import auth

    con = db.connect(settings.db_path)
    try:
        if args.action == "list":
            for u in auth.users(con):
                rights = "admin" if u["admin"] or u["navidrome_admin"] else u["permissions"] or "own lists"
                last = u["last_login"] or "never"
                state = "disabled (gone from Navidrome)" if u["disabled"] else f"last login {last}"
                print(f"{u['name']}\t{rights}\t{state}")
            return 0
        row = con.execute("SELECT id, admin, permissions FROM users WHERE name = ?", (args.name,)).fetchone()
        if row is None:
            raise SystemExit(f"No user {args.name} (they appear here after their first login).")
        if args.action == "admin":
            auth.set_rights(con, row["id"], not args.off, set(filter(None, row["permissions"].split(","))))
            print(f"{args.name} is {'no longer ' if args.off else ''}an admin.")
        else:
            auth.set_rights(con, row["id"], bool(row["admin"]), set(args.permissions))
            print(f"{args.name}: {', '.join(args.permissions) or 'no permissions'}.")
    except auth.AuthError as err:
        raise SystemExit(str(err)) from err
    finally:
        con.close()
    return 0


def _tags(args: argparse.Namespace, settings: Settings) -> int:
    """`tags normalize`: only with the jobs paused and none running (they write the same files). The old
    tags go to <data>/tag-backups/normalize-<time>.jsonl, the report next to it; a conflict goes to review."""
    import datetime
    import json

    from echolot import db
    from echolot.library import catalog, filing, review, tagging
    from echolot.settings import options

    if not settings.library_dir:
        raise SystemExit("No library configured (ECHOLOT_LIBRARY_DIR).")
    con = db.connect(settings.db_path)
    try:
        running = [r[0] for r in con.execute("SELECT name FROM jobs WHERE finished IS NULL")]
        if not args.dry_run and (not options.get(con, options.Jobs).paused or running):
            raise SystemExit(f"Pause the jobs first and let them finish (running: {', '.join(running) or 'none'}).")
        folder = settings.data_dir / "tag-backups"
        folder.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
        backup = None if args.dry_run else folder / f"normalize-{stamp}.jsonl"
        report = tagging.normalize(con, settings.library_dir, dry_run=args.dry_run, backup=backup, limit=args.limit)
        if not args.dry_run:
            paths = filing.Paths(settings.library_dir.parent)
            for c in report["conflicts"]:
                for key in c["keys"]:
                    review.recheck(con, paths, key, f"the file's tags name another song: '{c['tags']}'")
            catalog.refresh(con, settings.library_dir)
            fields = ", ".join(f"{n} {k}" for k, n in report["fields"].items())
            filing.event(con, paths, "retagged", settings.library_dir, reason=f"{report['changed']} files: {fields}")
        name = f"normalize-{stamp}{'-dry-run' if args.dry_run else ''}.json"
        (folder / name).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    finally:
        con.close()
    summary = {k: v for k, v in report.items() if k not in ("conflicts", "errors", "examples")}
    print(json.dumps(summary, ensure_ascii=False))
    print(f"{len(report['conflicts'])} conflicts, {len(report['errors'])} errors; report: {folder / name}")
    return 1 if report["errors"] else 0


def _config(args: argparse.Namespace, settings: Settings) -> int:
    from echolot import db
    from echolot.settings import configfile

    con = db.connect(settings.db_path)
    try:
        if args.action == "export":
            text = configfile.export_text(con)
            if args.file:
                Path(args.file).write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
            return 0
        text = Path(args.file).read_text(encoding="utf-8")
        try:
            diff = configfile.preview(con, text)
            if args.dry_run or not diff:
                sys.stdout.write(diff or "No changes.\n")
                return 0
            configfile.apply(con, text)
        except configfile.ConfigError as err:
            raise SystemExit(f"Not imported: {err}") from err
        sys.stdout.write(diff)
        print("Imported.")
    finally:
        con.close()
    return 0


def _secret(args: argparse.Namespace, settings: Settings) -> int:
    from echolot import db
    from echolot.settings import vault

    con = db.connect(settings.db_path)
    try:
        if args.action == "list":
            for s in vault.listing(con):
                print(f"{s['name']}\t{'set ' + s['updated'] if s['updated'] else 'not set'}\t{s['help']}")
            return 0
        v = vault.Vault.from_env(settings.data_dir)
        with con:
            if args.action == "set":
                value = sys.stdin.readline().rstrip("\n") if args.stdin else getpass.getpass(f"{args.name}: ")
                if not value:
                    raise SystemExit("Empty value, nothing stored.")
                v.set(con, args.name, value)
                print(f"{args.name} stored.")
            elif not v.delete(con, args.name):
                raise SystemExit(f"No secret {args.name}.")
            else:
                print(f"{args.name} deleted.")
    finally:
        con.close()
    return 0


def _jobs(args: argparse.Namespace, settings: Settings) -> int:
    """`jobs pause [--wait]`, `jobs resume`, `jobs status` (first line: paused: yes|no, for scripts). A pause
    reaches the running jobs within the worker's tick; --wait returns once none runs (1 after --timeout)."""
    import time

    from echolot import db
    from echolot.settings import options

    con = db.connect(settings.db_path)
    try:
        if args.action != "status":
            with con:
                options.update(con, options.Jobs, paused=args.action == "pause")
            print("Jobs paused." if args.action == "pause" else "Jobs resumed.")
        else:
            print(f"paused: {'yes' if options.get(con, options.Jobs).paused else 'no'}")
        end, said = time.monotonic() + args.timeout * 60, None
        while True:
            running = [r[0] for r in con.execute("SELECT name FROM jobs WHERE finished IS NULL ORDER BY name")]
            if args.action == "status" or not (args.action == "pause" and args.wait) or not running:
                if args.action == "status" or running:
                    print(f"running: {', '.join(running) or 'none'}")
                return 0
            if time.monotonic() > end:
                print(f"Still running after {args.timeout} min: {', '.join(running)}.")
                return 1
            if running != said:
                print(f"Waiting for {', '.join(running)} to end after the songs in progress...", flush=True)
                said = running
            time.sleep(5)
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="echolot", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the web dashboard")
    serve.add_argument("--host", help="listen address (default: ECHOLOT_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, help="listen port (default: ECHOLOT_PORT or 8490)")
    commands.add_parser("version", help="print the version")

    user = commands.add_parser("user", help="the users (Navidrome's accounts) and their rights")
    user_actions = user.add_subparsers(dest="action", required=True)
    user_actions.add_parser("list", help="list the users")
    admin = user_actions.add_parser("admin", help="make a user an admin (--off: no longer)")
    admin.add_argument("name")
    admin.add_argument("--off", action="store_true")
    permit = user_actions.add_parser("permit", help="set what a user who is no admin may do (none: nothing more)")
    permit.add_argument("name")
    permit.add_argument("permissions", nargs="*", choices=["review", "upload", "run"])

    config = commands.add_parser("config", help="the configuration as one YAML file (echolot.yml)")
    config_actions = config.add_subparsers(dest="action", required=True)
    export = config_actions.add_parser("export", help="write the configuration (no secrets)")
    export.add_argument("file", nargs="?", help="file to write (default: standard output)")
    imp = config_actions.add_parser("import", help="read a configuration file")
    imp.add_argument("file")
    imp.add_argument("--dry-run", action="store_true", help="only show what would change")

    secret = commands.add_parser("secret", help="encrypted credentials")
    secret_actions = secret.add_subparsers(dest="action", required=True)
    secret_actions.add_parser("list", help="which secrets are set (never their values)")
    s = secret_actions.add_parser("set", help="store a secret (asked for, or --stdin)")
    s.add_argument("name")
    s.add_argument("--stdin", action="store_true", help="read the value from stdin")
    d = secret_actions.add_parser("delete", help="delete a secret")
    d.add_argument("name")

    jobs = commands.add_parser("jobs", help="pause or resume the scheduled jobs, or show their state")
    jobs.add_argument("action", choices=["pause", "resume", "status"])
    jobs.add_argument("--wait", action="store_true", help="pause: wait until no job runs")
    jobs.add_argument("--timeout", type=int, default=60, help="minutes --wait waits at most (default 60)")

    tags = commands.add_parser("tags", help="the library files' tags")
    tag_actions = tags.add_subparsers(dest="action", required=True)
    norm = tag_actions.add_parser("normalize", help="give every file the tags of its song (jobs paused)")
    norm.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    norm.add_argument("--limit", type=int, default=0, help="only the first N files")

    args = parser.parse_args(argv)

    if args.command == "version":
        print(f"{__version__} ({COMMIT})" if COMMIT else __version__)
        return 0
    settings = Settings.from_env()
    if args.command != "serve":
        from echolot import db

        db.init(settings.db_path)
        handler = {"user": _user, "config": _config, "secret": _secret, "jobs": _jobs, "tags": _tags}[args.command]
        return handler(args, settings)

    import logging

    import uvicorn

    from echolot.web import create_app

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    uvicorn.run(
        create_app(settings), host=args.host or settings.host, port=args.port or settings.port, proxy_headers=True
    )
    return 0

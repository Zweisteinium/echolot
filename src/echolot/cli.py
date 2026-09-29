"""Command line: `echolot serve`, users, configuration file, secrets, `echolot version`."""

import argparse
import getpass
import sys
from pathlib import Path

from echolot import __version__
from echolot.config import Settings


def _password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Again: "):
        raise SystemExit("The two passwords differ.")
    return first


def _user(args: argparse.Namespace, settings: Settings) -> int:
    from echolot import db
    from echolot.settings import auth

    con = db.connect(settings.db_path)
    try:
        if args.action == "list":
            for u in auth.users(con):
                print(f"{u['name']}\tcreated {u['created']}\tlast login {u['last_login'] or 'never'}")
            return 0
        if args.action == "add":
            auth.add_user(con, args.name, _password(args))
            print(f"User {args.name} added.")
        else:
            user = auth.get_user(con, args.name)
            if user is None:
                raise SystemExit(f"No user {args.name}.")
            auth.set_password(con, user, _password(args))
            print(f"Password of {user.name} changed; its sessions ended.")
    except auth.AuthError as err:
        raise SystemExit(str(err)) from err
    finally:
        con.close()
    return 0


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
    from echolot import db
    from echolot.settings import options

    con = db.connect(settings.db_path)
    try:
        with con:
            options.update(con, options.Jobs, paused=args.action == "pause")
    finally:
        con.close()
    print("Jobs paused." if args.action == "pause" else "Jobs resumed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="echolot", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the web dashboard")
    serve.add_argument("--host", help="listen address (default: ECHOLOT_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, help="listen port (default: ECHOLOT_PORT or 8490)")
    commands.add_parser("version", help="print the version")

    user = commands.add_parser("user", help="users who can log in")
    user_actions = user.add_subparsers(dest="action", required=True)
    for action, help_ in (("add", "add a user"), ("passwd", "set a user's password")):
        a = user_actions.add_parser(action, help=help_)
        a.add_argument("name")
        a.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    user_actions.add_parser("list", help="list the users")

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

    jobs = commands.add_parser("jobs", help="pause or resume the scheduled jobs")
    jobs.add_argument("action", choices=["pause", "resume"])

    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0
    settings = Settings.from_env()
    if args.command != "serve":
        from echolot import db

        db.init(settings.db_path)
        handler = {"user": _user, "config": _config, "secret": _secret, "jobs": _jobs}[args.command]
        return handler(args, settings)

    import logging

    import uvicorn

    from echolot.web import create_app

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    uvicorn.run(
        create_app(settings), host=args.host or settings.host, port=args.port or settings.port, proxy_headers=True
    )
    return 0

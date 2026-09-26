"""Command line: `echolot serve`, `echolot version`."""

import argparse

from echolot import __version__
from echolot.config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="echolot", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the web dashboard")
    serve.add_argument("--host", help="listen address (default: ECHOLOT_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, help="listen port (default: ECHOLOT_PORT or 8490)")
    commands.add_parser("version", help="print the version")
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0

    import logging

    import uvicorn

    from echolot.web import create_app

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host=args.host or settings.host,
        port=args.port or settings.port,
        proxy_headers=True,
    )
    return 0

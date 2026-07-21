"""Command-line entry point for the local DB-connect agent."""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from . import __version__
from .db import Db, DbError
from .server import create_server

DEFAULT_PORT = 8521
DEFAULT_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oraplanviz-agent",
        description="Local DB-connect agent for the Oracle Execution Plan Visualizer.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on (default: %(default)s)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: %(default)s)")
    parser.add_argument(
        "--allow-origin",
        action="append",
        dest="allowed_origins",
        default=None,
        help="Allowed CORS origin (repeatable). Defaults to localhost dev origins.",
    )
    parser.add_argument("--token", default=None, help="Bearer token clients must supply. Generated if omitted.")
    parser.add_argument("--dsn", default=None, help="Oracle DSN to connect to on startup (e.g. host:port/service).")
    parser.add_argument("--user", default=None, help="Oracle username to connect with on startup.")
    parser.add_argument("--version", action="version", version=f"oraplanviz-agent {__version__}")
    return parser


def _print_banner(host: str, port: int, token: str, allowed_origins) -> None:
    url = f"http://{host}:{port}"
    print("=" * 72)
    print(f" oraplanviz-agent v{__version__}")
    print("=" * 72)
    print(f" Listening on:      {url}")
    print(f" Bearer token:      {token}")
    print(" Allowed origins:")
    for origin in allowed_origins:
        print(f"   - {origin}")
    print()
    print(" Paste the URL and token into the app's Connect panel.")
    print(" Security note: this agent binds to localhost only and requires the")
    print(" bearer token above for every request except /api/health. Credentials")
    print(" you provide are held in memory only and are never written to disk.")
    print("=" * 72)


def main(argv=None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    allowed_origins = args.allowed_origins if args.allowed_origins else list(DEFAULT_ALLOWED_ORIGINS)
    token = args.token if args.token else secrets.token_urlsafe(24)

    db = Db()

    if args.dsn and args.user:
        password = getpass.getpass(f"Password for {args.user}@{args.dsn}: ")
        try:
            db.connect(args.dsn, args.user, password)
            print(f"Connected to {args.dsn} as {args.user}.")
        except DbError as exc:
            print(f"Failed to connect on startup: {exc.message}", file=sys.stderr)

    server = create_server(db, token=token, allowed_origins=allowed_origins, port=args.port, host=args.host)

    _print_banner(args.host, args.port, token, allowed_origins)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        db.disconnect()
        server.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())

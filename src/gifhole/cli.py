"""Entry point: `gifhole` starts the server and opens the library."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from gifhole import store
from gifhole.app import configured_token as _configured_token
from gifhole.app import create_app, default_root

PACKAGE_DIR = Path(__file__).resolve().parent

DEFAULT_PORT = 8777

# How far to look for a free port. Sequential rather than random on purpose:
# the port ends up in a saved bookmarklet and in the token cookie's origin, so
# a port that stays the same across restarts is worth more than an unpredictable
# one. With 8777 held by something permanent you land on 8778 every time.
PORT_SEARCH = 50


def port_in_use(host: str, port: int) -> bool:
    """Whether something is already listening there.

    Checked before anything is printed or opened. Binding is left to uvicorn;
    this only decides whether to bother, so the race between the two is
    harmless: uvicorn still reports its own failure if it loses.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def next_free_port(host: str, start: int) -> int | None:
    for candidate in range(start + 1, start + 1 + PORT_SEARCH):
        if not port_in_use(host, candidate):
            return candidate
    return None


def resolve_port(host: str, requested: int | None) -> int:
    """Decide which port to serve on, before anything claims to be serving.

    Asking for a port explicitly means that port or nothing: silently serving
    somewhere else would be worse than failing, because whatever was pointed at
    the old one is now pointed at a stranger. Not asking means the default, and
    moving off it if it is busy, since the common case is a second gifhole or
    an unrelated service on 8777 and the user does not care which port they get.

    This all happens before anything is printed or opened. It used to bind
    late, so a failure still printed "serving:", opened a browser at a dead
    URL, and exited 0.
    """
    if requested is not None:
        if port_in_use(host, requested):
            print(f"gifhole: {host}:{requested} is already in use", file=sys.stderr, flush=True)
            spare = next_free_port(host, requested)
            if spare:
                print(f"         free: {spare}. Omit --port to move automatically", file=sys.stderr)
            raise SystemExit(1)
        return requested

    if not port_in_use(host, DEFAULT_PORT):
        return DEFAULT_PORT

    spare = next_free_port(host, DEFAULT_PORT)
    if spare is None:
        print(
            f"gifhole: {DEFAULT_PORT} is in use and nothing is free within "
            f"{PORT_SEARCH} of it; pick one with --port",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"gifhole: {DEFAULT_PORT} is in use, serving on {spare} instead", flush=True)
    return spare


def move(args) -> int:
    """Relocate the library. Files only; see store.move_library."""
    try:
        destination = store.move_library(args.root, args.destination)
    except (ValueError, OSError) as exc:
        print(f"gifhole: {exc}", file=sys.stderr)
        return 1

    print(f"moved the library to {destination}")
    # The root is a runtime argument, so nothing now points at the new place.
    # Saying so beats letting the next launch quietly build an empty library at
    # the old path.
    print("\nPoint gifhole at it, or the next run will start an empty library:")
    print(f"  export GIFHOLE_ROOT={destination}")
    print(f"  gifhole --root {destination}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="gifhole", description="local GIF library")
    parser.add_argument("--root", type=Path, default=default_root(), help="library directory")
    # No default here, so "not given" is distinguishable from "asked for 8777".
    # Asking for a port means that port or nothing; not asking means just work.
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"port to serve on (default {DEFAULT_PORT}, "
        "moving to the next free one if that is taken)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--reload", action="store_true", help="restart when the source changes (development)"
    )
    parser.add_argument(
        "--token",
        default="",
        help="require this token on every request (or set GIFHOLE_TOKEN). "
        "Off by default; needed if you expose gifhole beyond loopback",
    )
    parser.add_argument(
        "--read-token",
        default="",
        help="a second token that can look but not touch (or GIFHOLE_READ_TOKEN). "
        "Needs --token as well, or it would do nothing",
    )
    parser.add_argument(
        "--public-reads",
        action="store_true",
        help="let anyone browse without a token, while writes still need --token "
        "(or GIFHOLE_PUBLIC_READS=1). Needs --token as well",
    )
    # Optional on purpose: bare `gifhole` still means "serve the library", so
    # the subparser must not be required.
    commands = parser.add_subparsers(dest="command")
    mover = commands.add_parser("move", help="move the library to another directory")
    mover.add_argument("destination", type=Path, help="where to move it")
    args = parser.parse_args()

    if args.command == "move":
        raise SystemExit(move(args))

    port = resolve_port(args.host, args.port)

    url = f"http://{args.host}:{port}/"
    print(f"gifhole  library: {args.root / 'gifs'}\n        serving: {url}", flush=True)
    if not args.no_open:
        threading.Timer(0.8, webbrowser.open, (url,)).start()

    if args.token:
        # The reload path rebuilds the app in a subprocess and cannot be handed
        # arguments, so the token travels the same way --root does.
        os.environ["GIFHOLE_TOKEN"] = args.token
    if args.read_token:
        os.environ["GIFHOLE_READ_TOKEN"] = args.read_token
    if args.public_reads:
        os.environ["GIFHOLE_PUBLIC_READS"] = "1"
    if _configured_token(args.token):
        print(f"        access: token required, add ?token=... to {url} once", flush=True)

    common = {"host": args.host, "port": port, "log_level": "warning"}
    if not args.reload:
        uvicorn.run(
            create_app(
                args.root,
                token=args.token or None,
                read_token=args.read_token or None,
                public_reads=args.public_reads or None,
            ),
            **common,
        )
        return

    # The reloader rebuilds the app in a subprocess, so it can only be handed an
    # import string, and --root has to travel by environment rather than by
    # argument. Only .py files are watched: the static assets are read from disk
    # per request, so a restart would buy nothing there.
    os.environ["GIFHOLE_ROOT"] = str(args.root)
    uvicorn.run(
        "gifhole.app:create_app",
        factory=True,
        reload=True,
        reload_dirs=[str(PACKAGE_DIR)],
        **common,
    )


if __name__ == "__main__":
    main()

"""Entry point: `gifhole` starts the server and opens the library."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from gifhole import store
from gifhole.app import create_app, default_root

PACKAGE_DIR = Path(__file__).resolve().parent


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
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--reload", action="store_true", help="restart when the source changes (development)"
    )
    # Optional on purpose: bare `gifhole` still means "serve the library", so
    # the subparser must not be required.
    commands = parser.add_subparsers(dest="command")
    mover = commands.add_parser("move", help="move the library to another directory")
    mover.add_argument("destination", type=Path, help="where to move it")
    args = parser.parse_args()

    if args.command == "move":
        raise SystemExit(move(args))

    url = f"http://{args.host}:{args.port}/"
    print(f"gifhole  library: {args.root / 'gifs'}\n        serving: {url}")
    if not args.no_open:
        threading.Timer(0.8, webbrowser.open, (url,)).start()

    common = {"host": args.host, "port": args.port, "log_level": "warning"}
    if not args.reload:
        uvicorn.run(create_app(args.root), **common)
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

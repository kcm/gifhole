"""Entry point: `gifhole` starts the server and opens the library."""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser
from pathlib import Path

import uvicorn

from gifhole.app import create_app, default_root

PACKAGE_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(prog="gifhole", description="local GIF library")
    parser.add_argument("--root", type=Path, default=default_root(), help="library directory")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--reload", action="store_true", help="restart when the source changes (development)"
    )
    args = parser.parse_args()

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

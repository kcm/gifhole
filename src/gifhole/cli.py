"""Entry point: `gifhole` starts the server and opens the library."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from gifhole.app import create_app, default_root


def main() -> None:
    parser = argparse.ArgumentParser(prog="gifhole", description="local GIF library")
    parser.add_argument("--root", type=Path, default=default_root(), help="library directory")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/"
    print(f"gifhole  library: {args.root / 'gifs'}\n        serving: {url}")
    if not args.no_open:
        threading.Timer(0.8, webbrowser.open, (url,)).start()

    uvicorn.run(create_app(args.root), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

"""A real gifhole, in a real browser engine, on a virtual display.

Kept out of `tests/` on purpose. That suite is hermetic and runs in under two
seconds; this one needs browser engines and a running server, so it is opt-in:

    browser/run                 # all three engines, in Docker
    uv run pytest browser       # against locally installed Playwright

The engines come from the Playwright image, so this is also the only place the
UI is exercised on Linux rather than on the author's Mac.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOST = "127.0.0.1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def make_gif(path: Path) -> None:
    from PIL import Image, ImageDraw

    frames = []
    for i in range(6):
        frame = Image.new("RGB", (64, 48), (20, 20, 30))
        ImageDraw.Draw(frame).rectangle([i * 10, 0, i * 10 + 10, 48], fill=(230, 140, 40))
        frames.append(frame)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=80)


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """A gifhole serving one GIF, on its own port, torn down at the end.

    `GIFHOLE_URL` points at an already-running one instead. That is how
    `browser/run` works: gifhole needs Python 3.13 and the Playwright image
    ships 3.12, so the app runs in its own container (the real Linux image)
    and only the browsers live here. It also means this file does not care
    whether the server is local, containerised, or on another machine.
    """
    external = os.environ.get("GIFHOLE_URL")
    if external:
        yield external.rstrip("/")
        return

    root = tmp_path_factory.mktemp("library")
    (root / "gifs").mkdir()
    make_gif(root / "gifs" / "demo.gif")

    port = free_port()
    env = {**os.environ, "GIFHOLE_ROOT": str(root)}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "gifhole.app:create_app",
            "--host",
            HOST,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://{HOST}:{port}"
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError(f"server died:\n{proc.stdout.read().decode()}")
        try:
            with socket.create_connection((HOST, port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("server never came up")

    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def page(browser_name, playwright, server):
    """A page with console errors collected, since a silent one is the bug.

    Every failure this harness exists to catch showed up first as an uncaught
    exception and a control that did nothing: no toast, no network error, no
    sign at all unless the console was open. So the errors are recorded here
    and asserted on in the tests rather than left for a human to notice.
    """
    engine = getattr(playwright, browser_name)
    browser = engine.launch()
    ctx = browser.new_context()
    page = ctx.new_page()
    page.errors = []
    page.on("console", lambda m: m.type == "error" and page.errors.append(m.text))
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(server)
    page.wait_for_selector(".card", timeout=10_000)
    yield page
    browser.close()

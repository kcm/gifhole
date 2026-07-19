"""Putting a GIF on the clipboard as a file.

Why this exists: a browser cannot do it. The Clipboard API writes a fixed set
of MIME types, and `image/gif` is not one of them, so a page can only offer a
still PNG. Apps like Discord and Slack animate a pasted GIF because they
receive a *file* and upload it, exactly as if it had been copied in Finder.

gifhole already runs a local server on the user's machine, so it can write the
real file reference to the clipboard and give those apps what they want.

macOS goes through AppKit. Linux goes through wl-copy or xclip with a
`text/uri-list`, which is the same thing a file manager puts on the clipboard
when you copy a file. Both need a session to talk to, so neither works inside
the container, where there is no display at all.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)


def _load_appkit():
    if sys.platform != "darwin":
        return None
    try:
        from AppKit import NSURL, NSPasteboard
    except ImportError as exc:
        log.debug("AppKit unavailable: %s", exc)
        return None
    return NSPasteboard, NSURL


# Tool, and the arguments that make it write a uri-list rather than plain text.
# The trailing newline matters: the format is one URI per line and some readers
# discard an unterminated final entry.
LINUX_TOOLS = (
    ("wl-copy", ["--type", "text/uri-list"]),  # Wayland
    ("xclip", ["-selection", "clipboard", "-t", "text/uri-list"]),  # X11
)


def _linux_tool() -> tuple[str, list[str]] | None:
    # A tool on PATH is not enough: without a session to talk to it will fail
    # at run time, and reporting the feature as present would give a button
    # that always errors.
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return None
    for name, args in LINUX_TOOLS:
        path = shutil.which(name)
        if path:
            return path, args
    return None


def backend() -> str:
    """Which mechanism would be used: "appkit", "uri-list", or "" for none."""
    if _load_appkit() is not None:
        return "appkit"
    return "uri-list" if _linux_tool() is not None else ""


def available() -> bool:
    return bool(backend())


def copy_file(path: Path) -> None:
    """Place `path` on the clipboard as a file, the way a file manager does."""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if _load_appkit() is not None:
        return _copy_file_macos(path)
    tool = _linux_tool()
    if tool is None:
        raise RuntimeError(
            "no file clipboard here: needs macOS, or wl-copy/xclip in a graphical session"
        )
    return _copy_file_uri_list(path, tool)


def _copy_file_uri_list(path: Path, tool: tuple[str, list[str]]) -> None:
    """Hand over a `text/uri-list`, which is what a Linux file manager copies.

    Deliberately does not wait for the tool to exit. On X11 the process that
    owns a selection has to stay alive to serve it, so `xclip` keeps running
    by design and waiting for it hangs until a timeout. The first version of
    this did exactly that and reported a failure on every copy that had in
    fact succeeded. Still running after a moment is the success case; exiting
    non-zero quickly is the failure.
    """
    binary, args = tool
    uri = "file://" + quote(str(path))
    try:
        proc = subprocess.Popen(  # noqa: S603
            [binary, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise RuntimeError(f"{Path(binary).name} would not start: {exc}") from exc

    try:
        proc.stdin.write(f"{uri}\n".encode())
        proc.stdin.close()
    except (BrokenPipeError, OSError) as exc:
        proc.kill()
        raise RuntimeError(f"{Path(binary).name} closed early: {exc}") from exc

    try:
        code = proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        return  # holding the selection, which is the whole point
    if code != 0:
        detail = (proc.stderr.read() or b"").decode("utf-8", "replace")[:150]
        raise RuntimeError(f"{Path(binary).name} exited {code}: {detail}")


def _copy_file_macos(path: Path) -> None:
    """Writing the URL object (rather than an alias record) is what produces
    `NSFilenamesPboardType` and `public.file-url`, which is what paste targets
    look for when deciding to treat a paste as a file upload."""
    stack = _load_appkit()
    if stack is None:  # pragma: no cover - guarded by the caller
        raise RuntimeError("the file clipboard needs macOS")
    NSPasteboard, NSURL = stack

    board = NSPasteboard.generalPasteboard()
    board.clearContents()
    if not board.writeObjects_([NSURL.fileURLWithPath_(str(path))]):
        raise RuntimeError("the pasteboard rejected the file")

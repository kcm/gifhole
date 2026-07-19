"""Putting a GIF on the macOS pasteboard as a file.

Why this exists: a browser cannot do it. The Clipboard API writes a fixed set
of MIME types, and `image/gif` is not one of them, so a page can only offer a
still PNG. Apps like Discord and Slack animate a pasted GIF because they
receive a *file* and upload it, exactly as if it had been copied in Finder.

gifhole already runs a local server on the user's machine, so it can write the
real file reference to the pasteboard and give those apps what they want.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

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


def available() -> bool:
    return _load_appkit() is not None


def copy_file(path: Path) -> None:
    """Place `path` on the pasteboard as a file, the way Finder's Copy does.

    Writing the URL object (rather than an alias record) is what produces
    `NSFilenamesPboardType` and `public.file-url`, which is what paste targets
    look for when deciding to treat a paste as a file upload.
    """
    stack = _load_appkit()
    if stack is None:
        raise RuntimeError("the file clipboard needs macOS")
    NSPasteboard, NSURL = stack

    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    board = NSPasteboard.generalPasteboard()
    board.clearContents()
    if not board.writeObjects_([NSURL.fileURLWithPath_(str(path))]):
        raise RuntimeError("the pasteboard rejected the file")

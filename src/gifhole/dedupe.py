"""Spotting a GIF you already have.

Two signals, cheapest first:

* **sha256** of the file bytes. Exact, and it catches the common case outright:
  scraping the same page twice, or re-downloading a link you already grabbed.
* **dhash**, a perceptual hash of one frame. Catches the same GIF re-encoded,
  resized, or re-hosted, where the bytes differ but the picture does not.

Why not the `imagededup` package, which is the obvious reference for this: its
hashing methods are the standard published algorithms, but installing it pulls
in torch, torchvision, scikit-learn, scipy and matplotlib, several gigabytes,
almost all of it for the CNN methods we would not use. dhash below is the same
algorithm those libraries implement, in a few lines, with Pillow alone.

The comparison is deliberately loose. A near-duplicate is offered to the user
for confirmation, never dropped silently, so a false positive costs one click
while a miss costs a duplicate in the library forever.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

# Hamming distance between two 64-bit dhashes below which two GIFs are "the
# same picture". Measured, not guessed: over 30 resized pairs and 90 unrelated
# pairs of generated scenes, the same picture at a different size scored at
# most 13 (average 4.5) and different pictures never scored below 19. 12 sits
# in that gap, leaning towards catching duplicates: every match is shown for
# confirmation, so a false positive costs one click while a miss puts a
# duplicate in the library permanently.
NEAR_DISTANCE = 12

HASH_SIZE = 8

# A picture with no structure (a solid colour, a plain title card, a frame that
# is entirely one shade) produces an all-zero dhash, and so does every other
# flat picture, so they would all match each other. Below this spread between
# the lightest and darkest sampled pixel, there is nothing to compare and the
# GIF gets no perceptual hash at all: it can then only ever match exactly.
MIN_CONTRAST = 8


def content_hash(data: bytes) -> str:
    """Exact identity of the file. Two GIFs with this equal are the same file."""
    return hashlib.sha256(data).hexdigest()


def dhash_image(image: Image.Image, size: int = HASH_SIZE) -> int | None:
    """Difference hash: is each pixel brighter than the one to its right?

    Resizing to a fixed tiny grid is what makes it survive rescaling and
    re-compression, and comparing neighbours rather than absolute values is
    what makes it survive brightness shifts. Returns None when the image has
    too little contrast for the answer to mean anything.
    """
    small = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    if max(pixels) - min(pixels) < MIN_CONTRAST:
        return None
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | int(pixels[base + col] < pixels[base + col + 1])
    return bits


def _frame_order(count: int) -> list[int]:
    """Frames to try, best guess first, deduped and in range.

    Capped at a handful: this runs once per GIF on add and across the whole
    library on backfill, and a GIF that is flat in four places is flat.
    """
    wanted = [count // 3, count // 2, 0, count - 1]
    seen: list[int] = []
    for index in wanted:
        index = max(0, min(index, count - 1))
        if index not in seen:
            seen.append(index)
    return seen


def perceptual_hash(path: Path) -> str:
    """dhash of one representative frame, as hex. Empty string if unreadable.

    One frame, never a comparison across frames: two GIFs of the same scene cut
    at different lengths should still match, and animation-aware comparison
    would cost far more for a worse answer.

    Which frame is the only subtlety. Frame 0 is a poor default because plenty
    of GIFs open on a fade, a title card, or black, and those all hash alike.
    So this tries a third of the way in first and walks to other positions if
    that frame turns out to be flat, rather than giving up and leaving the GIF
    with no perceptual hash at all.
    """
    try:
        with Image.open(path) as img:
            count = getattr(img, "n_frames", 1)
            positions = [0] if count == 1 else _frame_order(count)
            for index in positions:
                img.seek(index)
                bits = dhash_image(img.convert("RGB"))
                if bits is not None:
                    return f"{bits:016x}"
            return ""
    except (OSError, ValueError, EOFError) as exc:
        # Never fatal: a GIF that cannot be hashed is simply never deduped.
        log.debug("could not hash %s: %s", path, exc)
        return ""


def distance(a: str, b: str) -> int:
    """Hamming distance between two hex dhashes; 64 (max) if either is missing."""
    if not a or not b or len(a) != len(b):
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def is_near(a: str, b: str, threshold: int = NEAR_DISTANCE) -> bool:
    return distance(a, b) <= threshold

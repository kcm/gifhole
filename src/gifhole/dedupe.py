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
MIN_CONTRAST = 12


# Prevent decompression bomb denial-of-service
Image.MAX_IMAGE_PIXELS = 25_000_000


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
    pixels = (
        list(small.get_flattened_data())
        if hasattr(small, "get_flattened_data")
        else list(small.getdata())
    )
    if max(pixels) - min(pixels) < MIN_CONTRAST:
        return None
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | int(pixels[base + col] < pixels[base + col + 1])
    return bits


# How many frames to sample across an animation. One frame was not enough: two
# encodes of the same GIF at different lengths (measured: 54 frames vs 29) put
# "a third of the way in" at different moments, so a single representative frame
# compared the wrong pictures and scored 22 (a miss) when the closest aligned
# pair scored 9. Ten frames each, matched on the closest pair, catches it. More
# frames also means a GIF that opens on a title card still gets live frames.
HASH_FRAMES = 10


def _sample_positions(count: int, k: int) -> list[int]:
    """`k` frame indices spread evenly from first to last, deduped and in range."""
    if count <= 1:
        return [0]
    k = min(k, count)
    return sorted({min(round(i * (count - 1) / (k - 1)), count - 1) for i in range(k)})


def perceptual_hash(path: Path) -> str:
    """Space-joined dhashes of several frames sampled across the animation.

    Multiple frames, matched on the closest pair (see distance), because two
    encodes of the same GIF cut to different lengths only line up at some
    moments, and a single-frame hash kept comparing moments that did not line
    up. Flat frames (title cards, fades) fall out via MIN_CONTRAST, so a GIF
    that opens on black still gets hashes from its live frames. Empty string
    when nothing hashable was found.
    """
    try:
        with Image.open(path) as img:
            w, h = img.size
            if w * h > 25_000_000 or w > 10_000 or h > 10_000:
                return ""
            count = min(getattr(img, "n_frames", 1), 5000)
            hashes = []
            for index in _sample_positions(count, HASH_FRAMES):
                # Per frame, not per file: some GIFs have one unreadable frame
                # (a truncated or malformed block), and losing that frame must
                # not throw away the nine good ones. A single-frame hash used to
                # return before reaching a bad frame; sampling several means we
                # have to step over it.
                try:
                    img.seek(index)
                    bits = dhash_image(img.convert("RGB"))
                except (OSError, ValueError, EOFError):
                    continue
                if bits is not None:
                    hashes.append(f"{bits:016x}")
            return " ".join(hashes)
    except (OSError, ValueError, EOFError) as exc:
        # Never fatal: a GIF that cannot be hashed is simply never deduped.
        log.debug("could not hash %s: %s", path, exc)
        return ""


def distance(a: str, b: str) -> int:
    """Smallest Hamming distance between any frame of `a` and any frame of `b`;
    64 (max) if either has no hash. Min-over-frames is what lets two encodes
    that align only at some moments still register as the same picture. Old
    single-frame hashes (one value, no space) still work, they just compare
    that one frame until re-hashed."""
    ha, hb = a.split(), b.split()
    if not ha or not hb:
        return 64
    best = 64
    for x in ha:
        xv = int(x, 16)
        for y in hb:
            bits = bin(xv ^ int(y, 16)).count("1")
            if bits < best:
                best = bits
                if best == 0:
                    return 0
    return best


def frame_ints(phash: str) -> list[int]:
    """Parse a stored space-joined phash into int frame hashes, once, so a
    library-wide scan doesn't re-parse hex on every comparison."""
    return [int(h, 16) for h in phash.split()]


def frames_near(
    a: list[int],
    b: list[int],
    threshold: int = NEAR_DISTANCE,
    confuser_hashes: set[int] | None = None,
) -> bool:
    """Whether frames of `a` match frames of `b`.

    Multi-frame consensus:
    - If both GIFs have multiple frames:
      - Returns True if any pair has distance <= 6 (high-confidence match), OR
      - Returns True if at least 2 frames of `a` each match a frame of `b`
        within distance <= min(threshold, 10).
    - If either GIF has only 1 frame:
      - Returns True if any pair has distance <= min(threshold, 8).
    - Frames present in `confuser_hashes` (learned from dismissed false-positive pairs)
      are ignored during comparison.
    """
    if not a or not b:
        return False

    fa = [x for x in a if x not in confuser_hashes] if confuser_hashes else a
    fb = [y for y in b if y not in confuser_hashes] if confuser_hashes else b
    if not fa or not fb:
        fa, fb = a, b

    if len(fa) >= 2 and len(fb) >= 2:
        frame_thresh = min(threshold, 10)
        matched_a = 0
        for x in fa:
            has_match = False
            for y in fb:
                d = (x ^ y).bit_count()
                if d <= 6:
                    return True
                if d <= frame_thresh:
                    has_match = True
            if has_match:
                matched_a += 1

        if matched_a < 2:
            return False

        matched_b = sum(1 for y in fb if any((x ^ y).bit_count() <= frame_thresh for x in fa))
        return matched_b >= 2

    single_thresh = min(threshold, 8)
    for x in fa:
        for y in fb:
            if (x ^ y).bit_count() <= single_thresh:
                return True
    return False


def is_near(
    a: str,
    b: str,
    threshold: int = NEAR_DISTANCE,
    confuser_hashes: set[int] | None = None,
) -> bool:
    return frames_near(frame_ints(a), frame_ints(b), threshold, confuser_hashes)


def matching_frame_hashes(a: str, b: str, threshold: int = NEAR_DISTANCE) -> list[str]:
    """Return hex frame hashes from `a` and `b` that matched within `threshold`.

    Used when a duplicate is dismissed to record confuser frames for feedback learning.
    """
    ha, hb = a.split(), b.split()
    matched = set()
    for x in ha:
        xv = int(x, 16)
        for y in hb:
            if (xv ^ int(y, 16)).bit_count() <= threshold:
                matched.add(x)
                matched.add(y)
    return sorted(matched)

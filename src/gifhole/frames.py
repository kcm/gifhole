"""Sampling representative frames out of a GIF.

Both metadata paths need still frames: OCR reads text off them, and the Claude
enrichment path sends them as images. Captions in a reaction GIF are usually
burned into every frame, so a handful of evenly spaced samples is plenty.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageSequence

# Vision wants enough pixels to resolve glyphs; GIFs are small, so scale up
# rather than down. Beyond this the OCR gain flattens and calls just get slower.
MIN_OCR_EDGE = 800
MAX_FRAME_EDGE = 1400


def sample_frames(path: Path, count: int = 3) -> list[Image.Image]:
    """Return up to `count` RGB frames spread evenly across the animation."""
    with Image.open(path) as img:
        total = getattr(img, "n_frames", 1)
        if total <= 1:
            return [img.convert("RGB")]
        # Spread across the whole animation, from just after the start to the
        # last frame. Skipping frame 0 is deliberate: it is often a title card
        # or a mostly-blank fade-in.
        #
        # The previous spacing put the final sample at count/(count+1) of the
        # way through, so with the default of three the last quarter was never
        # looked at, and a caption that only appears at the end was invisible
        # to both OCR and Claude. Punchlines land at the end.
        first, last = min(1, total - 1), total - 1
        if count <= 1:
            wanted = [last]
        else:
            span = last - first
            wanted = sorted({first + round(i * span / (count - 1)) for i in range(count)})
        out = []
        for index, frame in enumerate(ImageSequence.Iterator(img)):
            if index in wanted:
                out.append(frame.convert("RGB"))
            if len(out) == len(wanted):
                break
        return out or [img.convert("RGB")]


def upscale_for_ocr(frame: Image.Image) -> Image.Image:
    """Scale a small frame up so text is legible to the recognizer."""
    long_edge = max(frame.size)
    if long_edge >= MIN_OCR_EDGE:
        return frame
    factor = MIN_OCR_EDGE / long_edge
    return frame.resize(
        (round(frame.width * factor), round(frame.height * factor)),
        Image.LANCZOS,
    )


def to_png_bytes(frame: Image.Image, max_edge: int = MAX_FRAME_EDGE) -> bytes:
    """Encode a frame as PNG, bounded so enrichment payloads stay small."""
    if max(frame.size) > max_edge:
        factor = max_edge / max(frame.size)
        frame = frame.resize(
            (round(frame.width * factor), round(frame.height * factor)),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    frame.save(buf, format="PNG")
    return buf.getvalue()

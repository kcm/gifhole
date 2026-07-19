"""Reading burned-in text off GIF frames with the macOS Vision framework.

Local, free, offline, and good at the case that matters here: heavy display
faces with a contrasting stroke over a busy background. Everything degrades to
"no text found" when Vision is unavailable, so the rest of the app never has to
care whether OCR is present.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from gifhole.frames import sample_frames, upscale_for_ocr

log = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.4


@dataclass(frozen=True)
class OcrResult:
    text: str
    available: bool
    reason: str = ""


def _load_vision():
    """Import the Vision stack, or return None if this box cannot do OCR."""
    try:
        import Quartz
        import Vision
    except ImportError as exc:  # non-macOS, or pyobjc not installed
        log.debug("Vision unavailable: %s", exc)
        return None
    return Vision, Quartz


def vision_available() -> bool:
    return _load_vision() is not None


def _recognize(png_bytes: bytes) -> list[str]:
    stack = _load_vision()
    if stack is None:
        return []
    Vision, Quartz = stack

    from Foundation import NSData

    data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    source = Quartz.CGImageSourceCreateWithData(data, None)
    if source is None:
        return []
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        return []

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        log.warning("Vision request failed: %s", err)
        return []

    lines = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        best = candidates[0]
        if best.confidence() >= MIN_CONFIDENCE:
            lines.append(str(best.string()))
    return lines


def _tidy(lines: list[str]) -> str:
    """Collapse near-duplicate lines across frames into one caption string."""
    seen: dict[str, str] = {}
    for line in lines:
        cleaned = re.sub(r"\s+", " ", line).strip()
        if len(cleaned) < 2:
            continue
        key = re.sub(r"[^a-z0-9]", "", cleaned.lower())
        if key and key not in seen:
            seen[key] = cleaned
    return " ".join(seen.values())


def read_gif_text(path: Path, frames: int = 3) -> OcrResult:
    """OCR a GIF, merging text found across sampled frames."""
    if _load_vision() is None:
        return OcrResult("", available=False, reason="Vision framework unavailable")
    try:
        from gifhole.frames import to_png_bytes

        lines: list[str] = []
        for frame in sample_frames(path, frames):
            lines.extend(_recognize(to_png_bytes(upscale_for_ocr(frame))))
        return OcrResult(_tidy(lines), available=True)
    except Exception as exc:  # a malformed GIF must not take the request down
        log.warning("OCR failed for %s: %s", path.name, exc)
        return OcrResult("", available=False, reason=str(exc))

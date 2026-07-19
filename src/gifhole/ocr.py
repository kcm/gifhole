"""Reading burned-in text off GIF frames.

Two engines, both local, free and offline. macOS Vision is preferred where it
exists: it is markedly better at the case that matters here, heavy display
faces with a contrasting stroke over a busy background. Tesseract is the
fallback everywhere else, which is what makes this work on Linux and in the
container instead of the feature simply being absent.

Everything still degrades to "no text found" when neither is present, so the
rest of the app never has to care which engine ran, or whether one did.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gifhole.frames import sample_frames, upscale_for_ocr

log = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.4

# Tesseract reports 0-100 rather than 0-1. Set lower than the Vision threshold
# on purpose: it is less confident on stylised meme lettering even when it has
# read it correctly, and a missed caption costs more here than a stray word,
# since the text is only ever used to widen a search.
TESSERACT_MIN_CONFIDENCE = 35

# "Sparse text": find as much as possible without assuming a page layout, which
# is right for captions scattered top and bottom over a picture.
TESSERACT_PSM = "11"


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


def _tesseract() -> str | None:
    return shutil.which("tesseract")


def backend() -> str:
    """Which engine would run: "vision", "tesseract", or "" for none."""
    if _load_vision() is not None:
        return "vision"
    if _tesseract() is not None:
        return "tesseract"
    return ""


def available() -> bool:
    return bool(backend())


def vision_available() -> bool:
    """Kept for callers that specifically mean Vision, not OCR in general."""
    return _load_vision() is not None


def _recognize_tesseract(png_bytes: bytes) -> list[str]:
    """OCR one frame with Tesseract, via TSV so confidence can be honoured.

    Plain text output would be simpler but throws away the per-word confidence,
    and without it a busy frame contributes a line of punctuation noise to
    every GIF's searchable text.
    """
    binary = _tesseract()
    if binary is None:
        return []
    try:
        proc = subprocess.run(
            [binary, "stdin", "stdout", "--psm", TESSERACT_PSM, "tsv"],
            input=png_bytes,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("tesseract failed: %s", exc)
        return []
    if proc.returncode != 0:
        log.warning("tesseract exited %s: %s", proc.returncode, proc.stderr[:200])
        return []

    rows = proc.stdout.decode("utf-8", "replace").splitlines()
    if not rows:
        return []
    header = rows[0].split("\t")
    try:
        conf_at, text_at = header.index("conf"), header.index("text")
        line_key = [header.index(c) for c in ("block_num", "par_num", "line_num")]
    except ValueError:
        return []

    # Words come back one per row; regroup them into the lines they came from.
    lines: dict[tuple, list[str]] = {}
    for row in rows[1:]:
        cells = row.split("\t")
        if len(cells) <= text_at:
            continue
        word = cells[text_at].strip()
        if not word:
            continue
        try:
            confidence = float(cells[conf_at])
        except ValueError:
            continue
        if confidence < TESSERACT_MIN_CONFIDENCE:
            continue
        lines.setdefault(tuple(cells[i] for i in line_key), []).append(word)
    return [" ".join(words) for words in lines.values()]


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
    engine = backend()
    if not engine:
        return OcrResult(
            "", available=False, reason="no OCR engine (macOS Vision, or install tesseract)"
        )
    recognise = _recognize if engine == "vision" else _recognize_tesseract
    try:
        from gifhole.frames import to_png_bytes

        lines: list[str] = []
        for frame in sample_frames(path, frames):
            lines.extend(recognise(to_png_bytes(upscale_for_ocr(frame))))
        return OcrResult(_tidy(lines), available=True)
    except Exception as exc:  # a malformed GIF must not take the request down
        log.warning("OCR failed for %s: %s", path.name, exc)
        return OcrResult("", available=False, reason=str(exc))

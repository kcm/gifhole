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
    # What the engine read before the scoreboard/HUD filter, joined. Kept so the
    # console can show the cleanup (raw -> kept), which is the useful line, not a
    # count.
    raw: str = ""


def _load_vision():
    """Import the Vision stack, or return None if this box cannot do OCR."""
    try:
        import Quartz
        import Vision
    except ImportError as exc:  # non-macOS, or pyobjc not installed
        log.debug("Vision unavailable: %s", exc)
        return None
    return Vision, Quartz


class TesseractError(RuntimeError):
    """Tesseract could not read the frame, as opposed to finding no text.

    The distinction is the whole point. `read_gif_text` turns this into
    available=False, which app.py records as a failed job, which leaves the GIF
    eligible for a later Rescan. Returning an empty list instead would stamp
    ocr_at and exclude it from retry forever while reporting success, which is
    the bug the Vision path was fixed for and this path reintroduced.
    """


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


def _recognize_tesseract(png_bytes: bytes) -> list[str]:
    """OCR one frame with Tesseract, via TSV so confidence can be honoured.

    Plain text output would be simpler but throws away the per-word confidence,
    and without it a busy frame contributes a line of punctuation noise to
    every GIF's searchable text.
    """
    binary = _tesseract()
    if binary is None:  # pragma: no cover - guarded by the caller
        raise TesseractError("tesseract disappeared between the check and the call")
    try:
        proc = subprocess.run(
            [binary, "stdin", "stdout", "--psm", TESSERACT_PSM, "tsv"],
            input=png_bytes,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TesseractError(f"tesseract could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise TesseractError(f"tesseract exited {proc.returncode}: {detail}")

    rows = proc.stdout.decode("utf-8", "replace").splitlines()
    if not rows:
        # Genuinely nothing on the page is a valid answer, unlike the above.
        return []
    header = rows[0].split("\t")
    try:
        conf_at, text_at = header.index("conf"), header.index("text")
        line_key = [header.index(c) for c in ("block_num", "par_num", "line_num")]
    except ValueError as exc:
        raise TesseractError("tesseract TSV had no conf/text columns") from exc

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


# Includes y, so all-caps interjections with no true vowel ("WHY", "GYM")
# still read as words rather than as consonant fragments.
_VOWELS = frozenset("aeiouyAEIOUY")
_TIMER = re.compile(r"\d{1,3}:\d{2}")
# Punctuation to strip from an OCR token before judging it. The en/em dashes
# are written as escapes, not literals, so they read as data here (characters
# to remove) and do not trip the no-dash pre-commit hook.
_STRIP = ".,:;!?-'\"()[]{}\u2013\u2014\u2026"


def _strong_word(token: str) -> bool:
    """A token long enough to be a word rather than a scoreboard abbreviation.

    Four letters with a vowel is the anchor: "VAMOS", "OVER", "LEVEL". A real
    3-letter caption ("NO", "WHY", "RUN") is indistinguishable from a team code
    ("DOR", "RMA") in isolation, so length is what carries here, not a
    dictionary.
    """
    letters = re.sub(r"[^A-Za-z]", "", token)
    return len(letters) >= 4 and any(c in _VOWELS for c in letters)


def _noise_token(token: str) -> bool:
    """The stuff burned-in HUDs are made of: a clock, a bare number, or a lone
    character. Their presence in a line with no real word marks it as
    scoreboard rather than caption."""
    if _TIMER.fullmatch(token):
        return True
    stripped = token.strip(_STRIP)
    if not stripped:  # pure punctuation
        return True
    if stripped.isdigit():  # a bare score or number
        return True
    letters = re.sub(r"[^A-Za-z]", "", token)
    return len(letters) <= 1  # a lone letter, or a digit glued to one ("0L")


def _wordlike(token: str) -> bool:
    """Two or more letters with a vowel: the weak test for a clean short line
    that has no numbers or stray characters to give it away."""
    letters = re.sub(r"[^A-Za-z]", "", token)
    return len(letters) >= 2 and any(c in _VOWELS for c in letters)


def _looks_like_caption(line: str) -> bool:
    """Keep a line only if it reads as words, not as a clock or a scoreboard.

    OCR reads burned-in timers, scores and channel bugs with high confidence,
    so a confidence threshold never catches them; this does, lexically:

    * a strong word anchors the line, so "OVER 9000" and "LEVEL 99" survive;
    * failing that, any HUD noise token (a bare number, a clock, a lone
      character) drops it, so "89 73:30 DOR 0 RMA ES" and "0 - 1" go;
    * a clean short line with neither ("NO", "WHY") is kept on the weak test.

    Deliberately lenient once anchored: the text only widens a search, so a
    mangled caption is still worth keeping; a row of scoreboard tokens is not.
    """
    tokens = line.split()
    if not tokens:
        return False
    if any(_strong_word(t) for t in tokens):
        return True
    if any(_noise_token(t) for t in tokens):
        return False
    return any(_wordlike(t) for t in tokens)


def _tidy(lines: list[str]) -> str:
    """Collapse near-duplicate lines across frames into one caption string,
    dropping lines that read as scoreboard or timer noise rather than text."""
    seen: dict[str, str] = {}
    for line in lines:
        cleaned = re.sub(r"\s+", " ", line).strip()
        if len(cleaned) < 2 or not _looks_like_caption(cleaned):
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
        raw = " ".join(line.strip() for line in lines if line.strip())
        return OcrResult(_tidy(lines), available=True, raw=raw)
    except Exception as exc:  # a malformed GIF must not take the request down
        log.warning("OCR failed for %s: %s", path.name, exc)
        return OcrResult("", available=False, reason=str(exc))

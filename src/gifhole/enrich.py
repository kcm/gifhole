"""Optional Claude-powered descriptions, meme identification, and tags.

Strictly opt-in. Local OCR already runs on every GIF and needs no key; this
adds the two things OCR cannot give you: what is actually happening in the
frame, and which meme it is. Nothing here is imported unless the user asks for
enrichment, so the app keeps working with no API key and no network.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from gifhole.frames import sample_frames, to_png_bytes

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"

SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "One sentence describing what happens in the GIF.",
        },
        "meme_name": {
            "type": "string",
            "description": (
                "The well-known name of this meme if it is a recognizable one "
                "(e.g. 'this is fine', 'distracted boyfriend'); empty string if not."
            ),
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-8 lowercase single-word tags useful for searching.",
        },
    },
    "required": ["description", "meme_name", "tags"],
    "additionalProperties": False,
}

PROMPT = """These are frames sampled in order from a single animated GIF in a \
personal reaction-GIF library.

Describe it for search: what happens, which recognizable meme it is if any, and \
tags someone would actually type to find it. Prefer the emotion or reaction it \
conveys ("annoyed", "celebrate", "facepalm") over literal scene description. \
Do not transcribe on-screen text; that is captured separately."""


class EnrichError(Exception):
    """Enrichment could not run: missing package, key, or a failed call."""


def available() -> tuple[bool, str]:
    """Report whether enrichment can run, and why not when it cannot."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "the anthropic package is not installed (pip install 'gifhole[enrich]')"
    import os

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        # An `ant auth login` profile also works, so this is a hint, not a verdict.
        log.debug("no API key in env; relying on an ant auth profile if present")
    return True, ""


def describe_gif(path: Path, frames: int = 3) -> dict:
    """Ask Claude what a GIF shows. Returns {description, meme_name, tags}."""
    ok, why = available()
    if not ok:
        raise EnrichError(why)

    import anthropic

    images = [to_png_bytes(f) for f in sample_frames(path, frames)]
    if not images:
        raise EnrichError("could not read any frames from that GIF")

    import base64

    content: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(png).decode(),
            },
        }
        for png in images
    ]
    content.append({"type": "text", "text": PROMPT})

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        raise EnrichError(f"Claude call failed: {exc}") from exc

    if response.stop_reason == "refusal":
        raise EnrichError("Claude declined to describe this image")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EnrichError(f"unparseable response: {text[:120]}") from exc

    tags = [t.strip().lower() for t in data.get("tags", []) if t.strip()]
    if meme := data.get("meme_name", "").strip():
        # The meme's name is the single most useful search key it can give us.
        tags = [*meme.lower().split(), *tags]
    return {
        "description": data.get("description", "").strip(),
        "meme_name": meme,
        "tags": tags,
    }

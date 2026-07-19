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

# Tagging a library automatically is only useful if the vocabulary stays small.
# Left unconstrained a model invents a fresh near-synonym per GIF ("laughing",
# "laughter", "lol", "hilarious"), which is the same shelf-splitting problem
# autocomplete solves for humans, at machine speed. So the schema pins the
# choice to tags the library already uses, and allows only a couple of genuinely
# new ones per GIF. The enum is enforced by structured output, not by asking
# nicely, so an off-vocabulary tag cannot come back at all.
MAX_NEW_TAGS = 2
MAX_TAGS = 6


def build_schema(vocabulary: list[str], max_new: int = MAX_NEW_TAGS) -> dict:
    known: dict = {
        "type": "array",
        "maxItems": MAX_TAGS,
        "description": "Tags for this GIF, chosen from the library's existing vocabulary.",
    }
    # An empty enum is not valid JSON Schema, so a library with no tags yet gets
    # the unconstrained shape and builds its vocabulary from the new-tag budget.
    known["items"] = (
        {"type": "string", "enum": sorted(vocabulary)} if vocabulary else {"type": "string"}
    )
    return {
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
            "known_tags": known,
            "new_tags": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": max_new,
                "description": (
                    "Lowercase single-word tags that are NOT in the existing "
                    "vocabulary. Leave empty unless an existing tag genuinely "
                    "does not fit; a smaller vocabulary is more useful."
                ),
            },
        },
        "required": ["description", "meme_name", "known_tags", "new_tags"],
        "additionalProperties": False,
    }


PROMPT = """These are frames sampled in order from a single animated GIF in a \
personal reaction-GIF library.

Describe it for search: what happens, which recognizable meme it is if any, and \
how someone would look for it. Prefer the emotion or reaction it conveys \
("annoyed", "celebrate", "facepalm") over literal scene description. Do not \
transcribe on-screen text; that is captured separately.

Tagging matters more than completeness. Reuse the library's existing tags \
wherever one fits, even loosely: a library with 30 well-used tags is far more \
useful than one with 300 near-synonyms. Only propose a new tag when nothing \
existing would plausibly be typed to find this GIF."""


def vocabulary_note(vocabulary: list[str]) -> str:
    if not vocabulary:
        return "\n\nThe library has no tags yet, so propose the first few."
    return "\n\nTags already in use, in order of how often:\n" + ", ".join(vocabulary)


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


def describe_gif(path: Path, frames: int = 3, vocabulary: list[str] | None = None) -> dict:
    """Ask Claude what a GIF shows. Returns {description, meme_name, tags}.

    `vocabulary` is the library's existing tags, most-used first. Passing it
    keeps the tagging consistent instead of inventing a synonym per GIF.
    """
    vocabulary = vocabulary or []
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
    content.append({"type": "text", "text": PROMPT + vocabulary_note(vocabulary)})

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": build_schema(vocabulary)}},
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

    return merge_result(data, vocabulary)


def merge_result(data: dict, vocabulary: list[str]) -> dict:
    """Fold the model's answer into one tag list, dropping anything unusable.

    New tags are filtered rather than trusted: the enum only constrains
    `known_tags`, so `new_tags` is the one place a multi-word or duplicate tag
    can still get in.
    """
    known = {t.lower() for t in vocabulary}
    tags: list[str] = []

    def push(tag: str) -> None:
        tag = tag.strip().lower()
        # Single words only, matching split_tags() on the store side, so a tag
        # never arrives already broken in two.
        if not tag or " " in tag or tag in tags:
            return
        tags.append(tag)

    # meme_name is null rather than "" whenever the model has nothing, so it is
    # normalised here instead of assuming a string comes back.
    meme = (data.get("meme_name") or "").strip()
    for tag in data.get("known_tags") or []:
        push(tag)
    for tag in data.get("new_tags") or []:
        if tag.strip().lower() not in known:
            push(tag)
    description = (data.get("description") or "").strip()
    # The meme's name used to be split into tags, which made it searchable at
    # the cost of shedding junk into the vocabulary ("distracted", "boyfriend").
    # The description is a search key too, so it goes there instead.
    if meme and meme.lower() not in description.lower():
        description = f"{meme}: {description}" if description else meme
    return {"description": description, "meme_name": meme, "tags": tags}

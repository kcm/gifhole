"""Optional Gemini and Claude powered descriptions, meme identification, and tags.

Strictly opt-in. Local OCR already runs on every GIF and needs no key; this
adds the two things OCR cannot give you: what is actually happening in the
frame, and which meme it is. Nothing here is imported unless the user asks for
enrichment, so the app keeps working with no API key and no network.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from gifhole.frames import sample_frames, to_png_bytes

log = logging.getLogger(__name__)


def load_config() -> dict:
    """Read local config from config.json in GIFHOLE_ROOT, ~/.gifhole, or ~/.config/gifhole."""
    candidates = []
    if os.environ.get("GIFHOLE_CONFIG"):
        candidates.append(Path(os.environ["GIFHOLE_CONFIG"]))
    root_str = os.environ.get("GIFHOLE_ROOT")
    if root_str:
        candidates.append(Path(root_str) / "config.json")
    candidates.append(Path.home() / ".gifhole" / "config.json")
    candidates.append(Path.home() / ".config" / "gifhole" / "config.json")
    candidates.append(Path.cwd() / "config.json")

    for path in candidates:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.debug("could not parse %s: %s", path, exc)
    return {}


def gemini_api_key() -> str | None:
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GOOGLE_GENAI_API_KEY")
    )


def ollama_host() -> str:
    cfg = load_config().get("ollama") or {}
    host = os.environ.get("GIFHOLE_OLLAMA_URL") or os.environ.get("OLLAMA_HOST")
    if not host:
        if "host" in cfg:
            host = str(cfg["host"])
        elif "server" in cfg:
            server = str(cfg["server"]).strip()
            port = cfg.get("port", 11434)
            if ":" in server and not server.startswith(("http://", "https://")):
                host = f"http://{server}"
            elif server.startswith(("http://", "https://")):
                host = server if ":" in server.split("//", 1)[-1] else f"{server}:{port}"
            else:
                host = f"http://{server}:{port}"
        else:
            return "http://localhost:11434"
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host.rstrip("/")


def ollama_configured() -> bool:
    if os.environ.get("GIFHOLE_OLLAMA_URL") or os.environ.get("OLLAMA_HOST"):
        return True
    if os.environ.get("GIFHOLE_ENRICH_BACKEND") == "ollama":
        return True
    cfg = load_config()
    return "ollama" in cfg or "ollama_servers" in cfg


def fetch_ollama_models(host: str | None = None) -> list[dict]:
    """Return configured or dynamic vision models for Ollama."""
    cfg = load_config().get("ollama") or {}
    configured_models = cfg.get("models")
    if configured_models and isinstance(configured_models, list):
        models = []
        for m in configured_models:
            name = str(m).strip()
            if not name:
                continue
            mid = name if name.startswith("ollama/") else f"ollama/{name}"
            models.append({"id": mid, "name": f"Ollama: {name.removeprefix('ollama/')}"})
        if models:
            return models

    # Query dynamically from the server
    url = f"{host or ollama_host()}/api/tags"
    try:
        import httpx

        with httpx.Client(timeout=2.0) as client:
            res = client.get(url)
            if res.status_code != 200:
                return []
            raw_models = res.json().get("models", [])
            models = []
            for m in raw_models:
                name = m.get("name") or m.get("model")
                if not name:
                    continue
                caps = m.get("capabilities") or []
                is_vision = "vision" in caps
                if not is_vision:
                    details = m.get("details") or {}
                    fam = details.get("family", "").lower()
                    families = [f.lower() for f in (details.get("families") or [])]
                    low = name.lower()
                    is_vision = any(
                        v in low or v in fam or any(v in f for f in families)
                        for v in ("vision", "vl", "llava", "glimmer", "minicpm")
                    )
                if is_vision:
                    models.append({"id": f"ollama/{name}", "name": f"Ollama: {name}"})
            return models
    except Exception as exc:  # noqa: BLE001 - a listing failure falls back cleanly
        log.debug("could not dynamically list ollama models: %s", exc)
    return []


def ollama_available() -> tuple[bool, str]:
    if not ollama_configured():
        return False, "no Ollama host configured"
    models = fetch_ollama_models()
    if not models:
        return False, f"cannot connect or no vision models found at {ollama_host()}"
    return True, ""


def _detect_default_model() -> str:
    if os.environ.get("GIFHOLE_ENRICH_BACKEND") == "ollama":
        models = fetch_ollama_models()
        return models[0]["id"] if models else "ollama"
    if os.environ.get("GIFHOLE_ENRICH_BACKEND") == "gemini":
        return "gemini-3.6-flash"
    if os.environ.get("GIFHOLE_ENRICH_BACKEND") == "claude":
        return "claude-sonnet-5"
    if ollama_configured():
        models = fetch_ollama_models()
        if models:
            return models[0]["id"]
    if gemini_api_key():
        return "gemini-3.6-flash"
    return "claude-sonnet-5"


DEFAULT_MODEL = _detect_default_model()


def default_model() -> str:
    return os.environ.get("GIFHOLE_ENRICH_MODEL") or DEFAULT_MODEL


GEMINI_MODELS = [
    {"id": "gemini-3.6-flash", "name": "Gemini 3.6 Flash"},
    {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
    {"id": "gemini-3.7-flash", "name": "Gemini 3.7 Flash"},
    {"id": "gemini-3.5-flash", "name": "Gemini 3.5 Flash"},
    {"id": "gemini-flash-latest", "name": "Gemini Flash Latest"},
    {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
    {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
]

_models_cache: list[dict] | None = None


def fetch_gemini_models() -> list[dict]:
    """Query Google API for models accessible to this key (free or paid tier).

    Falls back to GEMINI_MODELS if the dynamic query fails or is offline.
    """
    key = gemini_api_key()
    if not key:
        return list(GEMINI_MODELS)
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    try:
        import httpx

        with httpx.Client(timeout=10.0) as client:
            res = client.get(url, headers={"x-goog-api-key": key})
            if res.status_code == 200:
                raw_models = res.json().get("models", [])
                models = []
                for m in raw_models:
                    mid = m.get("name", "").removeprefix("models/")
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" not in methods:
                        continue
                    low = mid.lower()
                    excluded = (
                        "tts",
                        "transcribe",
                        "lyria",
                        "robotics",
                        "audio",
                        "deep-research",
                        "customtools",
                    )
                    if any(ex in low for ex in excluded):
                        continue
                    if low.startswith("gemini") or low.startswith("gemma"):
                        models.append({"id": mid, "name": m.get("displayName") or mid})
                if models:
                    return models
    except Exception as exc:  # noqa: BLE001 - a listing failure falls back cleanly
        log.debug("could not dynamically list gemini models: %s", exc)
    return list(GEMINI_MODELS)


_models_cache: list[dict] | None = None
_models_cache_time: float = 0.0
MODELS_CACHE_TTL = 30.0


def list_models(force: bool = False) -> list[dict]:
    """The account's available models, as [{"id", "name"}], for the picker.

    Empty when enrichment cannot run (no key, no package), so the UI simply
    offers no choice. Cached for 30s to avoid unnecessary network round trips
    while still picking up local model changes or newly booted servers.
    """
    global _models_cache, _models_cache_time
    import time

    now = time.time()
    if not force and _models_cache is not None and (now - _models_cache_time) < MODELS_CACHE_TTL:
        return _models_cache
    ok, _ = available()
    if not ok:
        return []

    models: list[dict] = []
    if ollama_configured():
        o_ok, _ = ollama_available()
        if o_ok:
            models.extend(fetch_ollama_models())

    g_ok, _ = gemini_available()
    if g_ok:
        models.extend(fetch_gemini_models())

    c_ok, _ = claude_available()
    if c_ok:
        try:
            import anthropic

            client = anthropic.Anthropic()
            models.extend(
                {"id": m.id, "name": getattr(m, "display_name", None) or m.id}
                for m in client.models.list(limit=100)
            )
        except Exception as exc:  # noqa: BLE001 - a listing failure just means no picker
            log.debug("could not list anthropic models: %s", exc)

    _models_cache = models
    _models_cache_time = now
    return _models_cache


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
    # No `maxItems` anywhere below: structured output rejects it outright
    # ("For 'array' type, property 'maxItems' is not supported"), so the counts
    # are asked for in the descriptions and enforced in merge_result(). The
    # enum is the constraint that actually matters and that one is honoured.
    known: dict = {
        "type": "array",
        "description": (
            f"At most {MAX_TAGS} tags for this GIF, chosen from the library's existing vocabulary."
        ),
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
                "description": (
                    f"At most {max_new} lowercase single-word tags that are NOT "
                    "in the existing vocabulary. Leave empty unless an existing "
                    "tag genuinely does not fit; a smaller vocabulary is more "
                    "useful than a complete one."
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
transcribe on-screen text; that is captured separately. Write the description \
in plain prose with no dashes as punctuation: it is stored in the user's own \
library and they edit it by hand.

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


def gemini_available() -> tuple[bool, str]:
    if gemini_api_key():
        return True, ""
    try:
        from google import genai

        client = genai.Client()
        if client:
            return True, ""
    except Exception:
        pass
    return False, "no Gemini API key. Set GEMINI_API_KEY or GOOGLE_API_KEY"


def claude_available() -> tuple[bool, str]:
    try:
        import anthropic
    except ImportError:
        return False, "the anthropic package is not installed (pip install 'gifhole[enrich]')"

    try:
        client = anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001 - any construction failure means unusable
        log.debug("anthropic client would not construct: %s", exc)
        return False, "no API key. Set ANTHROPIC_API_KEY or run `ant auth login`"

    if not (client.api_key or client.auth_token or getattr(client, "credentials", None)):
        return False, "no API key. Set ANTHROPIC_API_KEY or run `ant auth login`"
    return True, ""


def available() -> tuple[bool, str]:
    """Report whether enrichment can actually run, and why not when it cannot."""
    if ollama_configured():
        o_ok, _ = ollama_available()
        if o_ok:
            return True, ""
    g_ok, _ = gemini_available()
    if g_ok:
        return True, ""
    c_ok, _ = claude_available()
    if c_ok:
        return True, ""
    if ollama_configured():
        _, why = ollama_available()
        return False, why
    return False, "no LLM configured. Set GEMINI_API_KEY, ANTHROPIC_API_KEY, or OLLAMA_HOST"


MAX_GEMINI_ENUM_ITEMS = 40


def build_gemini_schema(vocabulary: list[str], max_new: int = MAX_NEW_TAGS) -> dict:
    # Google GenAI API responseSchema can reject large enums (> 50-100 items)
    # with 400 Invalid Argument. We cap the enum items to the most-frequent tags
    # (vocabulary is ordered by frequency), while leaving the full list in the prompt.
    capped_vocab = vocabulary[:MAX_GEMINI_ENUM_ITEMS] if vocabulary else []
    schema = build_schema(capped_vocab, max_new)
    return {k: v for k, v in schema.items() if k != "additionalProperties"}


def _describe_with_gemini(
    images: list[bytes],
    vocabulary: list[str],
    model: str = "gemini-3.6-flash",
) -> dict:
    import base64

    import httpx

    key = gemini_api_key()
    prompt_text = PROMPT + vocabulary_note(vocabulary)

    if not key:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client()
            parts = [types.Part.from_bytes(data=png, mime_type="image/png") for png in images]
            parts.append(types.Part.from_text(text=prompt_text))
            response = client.models.generate_content(
                model=model,
                contents=parts,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=build_gemini_schema(vocabulary),
                ),
            )
            text = response.text or ""
            data = json.loads(text)
            return merge_result(data, vocabulary)
        except Exception as exc:
            raise EnrichError(f"Gemini call failed: {exc}") from exc

    parts: list[dict] = [
        {
            "inline_data": {
                "mime_type": "image/png",
                "data": base64.standard_b64encode(png).decode("ascii"),
            }
        }
        for png in images
    ]
    parts.append({"text": prompt_text})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": build_gemini_schema(vocabulary),
        },
    }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        with httpx.Client(timeout=60.0) as client:
            res = client.post(
                url,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json=payload,
            )
    except Exception as exc:
        raise EnrichError(f"Gemini call failed: {exc}") from exc

    if res.status_code != 200:
        try:
            err_msg = res.json().get("error", {}).get("message", res.text)
        except Exception:
            err_msg = res.text
        raise EnrichError(f"Gemini API error ({res.status_code}): {err_msg}")

    body = res.json()
    candidates = body.get("candidates") or []
    if not candidates:
        raise EnrichError("Gemini returned no candidates")

    candidate = candidates[0]
    if candidate.get("finishReason") == "SAFETY":
        raise EnrichError("Gemini declined to describe this image due to safety filters")

    parts_resp = candidate.get("content", {}).get("parts", [])
    text = next((p["text"] for p in parts_resp if "text" in p), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EnrichError(f"unparseable Gemini response: {text[:120]}") from exc

    return merge_result(data, vocabulary)


def _describe_with_claude(
    images: list[bytes],
    vocabulary: list[str],
    model: str = "claude-sonnet-5",
) -> dict:
    import base64

    import anthropic

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
            model=model,
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


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        text = text[s : e + 1]
    return json.loads(text)


def _describe_with_ollama(
    images: list[bytes],
    vocabulary: list[str],
    model: str,
) -> dict:
    import base64

    import httpx

    actual_model = model
    if actual_model.startswith("ollama/"):
        actual_model = actual_model.removeprefix("ollama/")
    elif actual_model.startswith("ollama:"):
        actual_model = actual_model.removeprefix("ollama:")

    host = ollama_host()
    b64_images = [base64.standard_b64encode(png).decode("ascii") for png in images]
    prompt_text = PROMPT + vocabulary_note(vocabulary)
    schema = build_schema(vocabulary)

    payload = {
        "model": actual_model,
        "stream": False,
        "format": schema,
        "messages": [
            {
                "role": "user",
                "content": prompt_text,
                "images": b64_images,
            }
        ],
    }

    try:
        with httpx.Client(timeout=120.0) as client:
            res = client.post(f"{host}/api/chat", json=payload)
            # Some Ollama runners (like MLX) do not support the format/grammar constraint (501).
            # Fall back to prompt-instructed JSON output.
            if res.status_code == 501 or "structured output is unavailable" in res.text:
                fallback_prompt = (
                    prompt_text
                    + "\n\nRespond ONLY with a valid JSON object matching this schema:\n"
                    "{\n"
                    '  "description": "One sentence describing what happens in the GIF.",\n'
                    '  "meme_name": "The meme name if recognizable, else empty string.",\n'
                    '  "known_tags": ["at most 6 tags from existing vocabulary"],\n'
                    '  "new_tags": ["at most 2 new tags"]\n'
                    "}\n"
                    "Do not include any commentary or markdown outside the JSON."
                )
                payload["messages"][0]["content"] = fallback_prompt
                del payload["format"]
                res = client.post(f"{host}/api/chat", json=payload)
    except Exception as exc:
        raise EnrichError(f"Ollama call failed: {exc}") from exc

    if res.status_code != 200:
        raise EnrichError(f"Ollama API error ({res.status_code}): {res.text[:160]}")

    body = res.json()
    content = body.get("message", {}).get("content", "")
    try:
        data = _extract_json(content)
    except Exception as exc:
        raise EnrichError(f"unparseable Ollama response: {content[:120]}") from exc

    return merge_result(data, vocabulary)


def describe_gif(
    path: Path,
    frames: int = 3,
    vocabulary: list[str] | None = None,
    model: str | None = None,
) -> dict:
    """Ask an LLM (Gemini, Claude, Ollama) what a GIF shows.

    Returns {description, meme_name, tags}.

    `vocabulary` is the library's existing tags, most-used first. Passing it
    keeps the tagging consistent instead of inventing a synonym per GIF.
    `model` overrides the default for this one call (the picker's choice).
    """
    vocabulary = vocabulary or []
    model = model or default_model()
    ok, why = available()
    if not ok:
        raise EnrichError(why)

    images = [to_png_bytes(f) for f in sample_frames(path, frames)]
    if not images:
        raise EnrichError("could not read any frames from that GIF")

    if model.startswith("ollama/") or model.startswith("ollama:"):
        return _describe_with_ollama(images, vocabulary, model)
    if model.startswith("gemini") or model.startswith("gemma"):
        return _describe_with_gemini(images, vocabulary, model)
    if model.startswith("claude"):
        return _describe_with_claude(images, vocabulary, model)
    if ollama_configured():
        return _describe_with_ollama(images, vocabulary, model)
    return _describe_with_claude(images, vocabulary, model)


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
    for tag in (data.get("known_tags") or [])[:MAX_TAGS]:
        push(tag)
    # Truncated here rather than in the schema: structured output does not
    # support maxItems, so an over-eager answer has to be trimmed on arrival.
    fresh = [t for t in (data.get("new_tags") or []) if t.strip().lower() not in known]
    for tag in fresh[:MAX_NEW_TAGS]:
        push(tag)
    description = (data.get("description") or "").strip()
    # The meme's name used to be split into tags, which made it searchable at
    # the cost of shedding junk into the vocabulary ("distracted", "boyfriend").
    # The description is a search key too, so it goes there instead.
    if meme and meme.lower() not in description.lower():
        description = f"{meme}: {description}" if description else meme
    return {"description": description, "meme_name": meme, "tags": tags}

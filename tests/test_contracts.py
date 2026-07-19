"""Couplings the tools cannot see, checked cheaply.

The frontend has no bundler and no type checker on purpose, which means a
handful of contracts are held together by matching strings across files and
nothing complains when one side moves. Two real bugs in this project came from
exactly that, so they are asserted here rather than rediscovered by hand:

* `$("#id")` in app.js pointing at markup that no longer exists.
* `job.kind === "..."` in app.js naming a kind app.py never submits, which
  silently stopped finished descriptions from refreshing their card.

These run in a second and need no browser, so they are the cheap half of
"verify through the real interface", not a replacement for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "gifhole" / "static"
APP_PY = Path(__file__).resolve().parent.parent / "src" / "gifhole" / "app.py"

HTML = (STATIC / "index.html").read_text()
JS = (STATIC / "app.js").read_text()
CSS = (STATIC / "style.css").read_text()
PY = APP_PY.read_text()

# Ids the JS looks up, and ids the markup actually defines.
REFERENCED_IDS = set(re.findall(r"""\$\(\s*["']#([A-Za-z0-9_-]+)["']\s*\)""", JS)) | set(
    re.findall(r"""getElementById\(\s*["']([A-Za-z0-9_-]+)["']\s*\)""", JS)
)
DEFINED_IDS = set(re.findall(r"""\bid=["']([A-Za-z0-9_-]+)["']""", HTML))


def test_every_id_the_js_looks_up_exists_in_the_html():
    """A missing id makes $() return null and the next line throws."""
    missing = sorted(REFERENCED_IDS - DEFINED_IDS)
    assert not missing, f"app.js looks up ids that index.html does not define: {missing}"


def test_the_html_defines_no_ids_nothing_uses():
    """Not fatal, but a leftover id is usually the remains of a removed feature."""
    unused = sorted(DEFINED_IDS - REFERENCED_IDS)
    # These are addressed by CSS or by the user, not by a $() lookup.
    expected = {"graburl", "file", "sort", "search", "theme", "jobs", "tags", "grid", "empty"}
    assert not (set(unused) - expected), (
        f"unused ids in index.html: {sorted(set(unused) - expected)}"
    )


def test_the_job_kinds_the_ui_watches_are_kinds_the_server_submits():
    """The bug this exists for: renaming the enrich job to "describe" left the
    grid testing the old name, so a finished description never refreshed."""
    submitted = set(re.findall(r"""jobs\.submit\(\s*["']([a-z]+)["']""", PY))
    watched = set(re.findall(r"""job\.kind\s*===\s*["']([a-z]+)["']""", JS))
    unknown = sorted(watched - submitted)
    assert not unknown, (
        f"app.js watches job kinds app.py never submits: {unknown} (it submits {sorted(submitted)})"
    )


def test_the_capabilities_the_ui_reads_are_ones_the_server_sends():
    """A capability that is never sent reads as undefined, which is falsy, so
    the feature silently disables itself instead of failing loudly."""
    block = PY[PY.index('"capabilities"') : PY.index('"capabilities"') + 500]
    sent = set(re.findall(r"""["']([a-z_]+)["']\s*:""", block)) | {"capabilities"}
    read = set(re.findall(r"""capabilities\.([a-z_]+)""", JS))
    unknown = sorted(read - sent)
    assert not unknown, f"app.js reads capabilities the server never sends: {unknown}"


@pytest.mark.parametrize("path", sorted(STATIC.glob("*.js")))
def test_no_javascript_uses_innerhtml_with_interpolation(path):
    """The rule from AGENTS.md, enforced: values go in as textContent, never
    interpolated into markup, or a filename or tag becomes an injection."""
    text = path.read_text()
    bad = [
        line.strip()
        for line in text.splitlines()
        if "innerHTML" in line and ("${" in line or " + " in line)
    ]
    assert not bad, f"{path.name} interpolates into innerHTML: {bad}"


def test_every_class_the_js_toggles_is_styled():
    """A class the JS sets but no stylesheet mentions usually means a rename
    landed on one side only, and the state then has no visible effect."""
    toggled = set(re.findall(r"""classList\.(?:add|toggle)\(\s*["']([a-z-]+)["']""", JS))
    missing = sorted(c for c in toggled if f".{c}" not in CSS)
    assert not missing, f"classes toggled in app.js but absent from style.css: {missing}"


# -- repo hygiene ------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".md", ".toml", ".yml", ".rb"}


def tracked_text_files() -> list[Path]:
    import subprocess

    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    return [REPO / f for f in out if Path(f).suffix in TEXT_SUFFIXES]


def test_no_em_or_en_dashes_anywhere():
    """A standing style rule, enforced rather than remembered.

    Generated text is the reason this is mechanical: a model wrote an em-dash
    into a description here, and prose review is exactly the thing that gets
    skipped when the diff is large.
    """
    offenders = []
    for path in tracked_text_files():
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if "—" in line or "–" in line:
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()[:70]}")
    assert not offenders, "em/en dashes found:\n" + "\n".join(offenders[:15])


# Shapes worth refusing outright. Deliberately narrow: a scanner that cries
# wolf gets disabled, and this only has to catch the credential types this
# project actually handles.
SECRET_PATTERNS = {
    "Anthropic API key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{20,}",
    "AWS access key": r"AKIA[0-9A-Z]{16}",
    "private key block": r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY",
}


def test_no_credentials_are_committed():
    """An API key was pasted into this project's development twice. Nothing
    stopped it reaching a commit except noticing, which does not scale."""
    found = []
    for path in tracked_text_files():
        text = path.read_text(errors="ignore")
        for label, pattern in SECRET_PATTERNS.items():
            if re.search(pattern, text):
                found.append(f"{label} in {path.relative_to(REPO)}")
    assert not found, "possible credentials committed:\n" + "\n".join(found)

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

Deliberately only project contracts. Prose style and credential scanning are
personal preferences and belong in a global git hook, not in a shared repo
where they would be imposed on everyone contributing.
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


# -- fail closed -------------------------------------------------------------

# Every GET route, classified deliberately. Read routes are safe to expose with
# --public-reads; writer-only ones are not, because they cost something: an
# outbound request, money, or the host's bandwidth.
#
# This exists because WRITER_ONLY_PATHS is hand-maintained, so a new expensive
# GET would otherwise default to public and nothing would notice. Adding a route
# breaks this test on purpose: decide which side it belongs on.
PUBLIC_READ_ROUTES = {
    "/",
    "/gifs/{filename}",
    "/api/gifs",
    "/api/jobs",
    "/api/library",
    "/api/trash",
    "/api/duplicates",
}
WRITER_ONLY_READ_ROUTES = {
    # Fetches a URL the caller chooses: public would mean an open proxy.
    "/api/preview",
}


def test_every_read_route_is_classified():
    import tempfile

    from gifhole.app import create_app, needs_a_writer

    with tempfile.TemporaryDirectory() as root:
        app = create_app(root, auto_ocr=False)
        gets = {
            route.path
            for route in app.routes
            if "GET" in (getattr(route, "methods", None) or set())
            and not route.path.startswith("/static")
        }

    classified = PUBLIC_READ_ROUTES | WRITER_ONLY_READ_ROUTES
    unclassified = sorted(gets - classified)
    assert not unclassified, (
        f"new GET route(s) {unclassified}: add to PUBLIC_READ_ROUTES if safe to expose "
        "with --public-reads, or to WRITER_ONLY_PATHS in app.py if it costs anything"
    )
    stale = sorted(classified - gets)
    assert not stale, f"classified routes that no longer exist: {stale}"

    # And the classification must match what the guard actually does.
    for path in PUBLIC_READ_ROUTES:
        assert not needs_a_writer("GET", path), f"{path} is listed as public but is guarded"
    for path in WRITER_ONLY_READ_ROUTES:
        assert needs_a_writer("GET", path), f"{path} is listed as writer-only but is not guarded"

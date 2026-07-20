"""Cross-engine checks for the failures this project keeps having.

Not a UI tour. Each test here corresponds to a bug that shipped: a control that
looked live and did nothing, an error body read as data, a fragment that threw.
They all share a shape, which is why they were all invisible: an uncaught
exception, and a button that silently declines to work.

Run against chromium, firefox and webkit. Note that webkit is not Safari; it
catches a missing API or a parse error, not Safari's own quirks.
"""

from __future__ import annotations

import base64
import json

import pytest


def test_the_library_panel_opens(page):
    """It did not. `body` was declared inside the try and used outside it, so
    the panel threw a ReferenceError and stayed hidden, taking bulk describe,
    find duplicates, trash, clear-the-library and the bookmarklet with it. The
    console was the only place that said so."""
    page.click("#librarybtn")
    panel = page.locator("#library")
    panel.wait_for(state="visible", timeout=5_000)
    assert "gifhole" in page.text_content("#libsummary")
    assert page.errors == []


def test_the_grid_renders_and_stays_quiet(page):
    """A failed /api/jobs used to leave a permanently blank wall: the error
    body parsed as JSON, so nothing threw where anyone would see it."""
    assert page.locator(".card").count() >= 1
    assert page.errors == []


def test_the_help_panel_opens_from_the_keyboard(page):
    """Shortcuts are bare letters, so they are the first thing an engine
    difference breaks."""
    page.keyboard.press("?")
    page.locator("#help").wait_for(state="visible", timeout=5_000)
    assert page.errors == []


def test_the_trash_panel_opens_from_the_keyboard(page):
    page.keyboard.press("T")
    page.locator("#trash").wait_for(state="visible", timeout=5_000)
    assert page.errors == []


def test_search_filters_without_throwing(page):
    page.fill("#search", "definitely-not-a-gif-here")
    page.wait_for_timeout(400)
    assert page.locator(".card").count() == 0
    page.fill("#search", "")
    page.wait_for_timeout(400)
    assert page.locator(".card").count() >= 1
    assert page.errors == []


@pytest.mark.parametrize("fragment", ["#add=null", "#add=%7Bbroken", "#add=%E0%A4%A"])
def test_a_malformed_add_fragment_is_survivable(page, server, fragment):
    """A null payload threw on `payload.page`, uncaught and async, so nothing
    reported it. A bad fragment also used to stay in the address bar and
    re-fire on every reload."""
    page.goto(server + "/" + fragment)
    page.wait_for_timeout(600)
    assert page.errors == [], f"{fragment} threw: {page.errors}"
    assert "#add=" not in page.url, "a rejected fragment must not stay in the bar"


def test_the_theme_picker_survives_every_skin(page):
    """Seven skins, each a block of CSS variables. A skin that breaks the
    layout is only visible by switching to it. The picker lives in the library
    panel now, so open it first."""
    page.click("#librarybtn")
    page.locator("#theme").wait_for(state="visible", timeout=5_000)
    for value in ["fark", "zombo", "webvan", "petsdotcom", "altavista", "linkedin", "memepool"]:
        page.select_option("#theme", value)
        page.wait_for_timeout(120)
        assert page.get_attribute("html", "data-theme") == value
    assert page.errors == []


def test_a_queued_batch_can_be_stopped_from_the_console(page, server):
    """A describe-all is real money, so a queued batch must stay stoppable. With
    the always-on rail gone, that control lives in the console: it appears when
    jobs are queued and POSTs the cancel the running one never sees.

    The jobs poll is stubbed to a queued batch; a real one would need an API
    key and the network.
    """
    cancelled = {"hit": False}

    def jobs(route):
        route.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "jobs": [
                        {
                            "id": 1,
                            "kind": "describe",
                            "label": "a.gif",
                            "status": "running",
                            "detail": "",
                            "done": 0,
                            "total": 0,
                            "created_at": 0,
                        },
                        {
                            "id": 2,
                            "kind": "describe",
                            "label": "b.gif",
                            "status": "queued",
                            "detail": "",
                            "done": 0,
                            "total": 0,
                            "created_at": 0,
                        },
                    ],
                    "active": 2,
                    "capabilities": {"ocr": True, "enrich": True, "read_only": False},
                }
            ),
        )

    def cancel(route):
        cancelled["hit"] = True
        route.fulfill(
            content_type="application/json", body=json.dumps({"ok": True, "cancelled": 1})
        )

    page.route("**/api/jobs", jobs)
    page.route("**/api/jobs/cancel", cancel)
    page.goto(server)
    page.keyboard.press("Backquote")
    page.locator("#console").wait_for(state="visible", timeout=5_000)

    stop = page.locator("#consolestop")
    stop.wait_for(state="visible", timeout=5_000)
    assert "1 queued" in page.locator("#consolequeue").inner_text()
    stop.click()
    # The toast confirms the cancel round-tripped, which is a real wait rather
    # than a fixed sleep.
    page.locator("#toast", has_text="stopped 1 queued").wait_for(state="visible", timeout=5_000)
    assert cancelled["hit"], "the console stop must POST the cancel"
    assert page.errors == []


def test_the_console_toggles_with_the_backtick_key(page):
    """` / ~ brings up the process console, a la a GUI editor's terminal, and
    toggles it away again. A view, not an action, so a guest may open it too."""
    console = page.locator("#console")
    assert console.is_hidden()
    page.keyboard.press("Backquote")
    console.wait_for(state="visible", timeout=5_000)
    page.keyboard.press("Backquote")
    console.wait_for(state="hidden", timeout=5_000)
    assert page.errors == []


def test_the_backtick_does_not_toggle_the_console_while_typing(page):
    """Shortcuts are bare keys, so the typing guard is load-bearing: a backtick
    typed into the search box must land as a character, not open the console."""
    page.click("#search")
    page.keyboard.press("Backquote")
    page.wait_for_timeout(150)
    assert page.locator("#console").is_hidden(), "console must stay closed while typing"
    assert "`" in page.input_value("#search")
    assert page.errors == []


def test_the_console_tails_the_log_by_cursor(page, server):
    """The console back-fills what is buffered when opened, then follows new
    lines by cursor without replaying the ones it already has."""
    seq = {"n": 0}

    def handler(route):
        # First poll (since=0) returns two lines; later polls return one new
        # line exactly once, so a replay would show duplicates and fail below.
        url = route.request.url
        since = int(url.split("since=")[1]) if "since=" in url else 0
        if since == 0:
            seq["n"] = 2
            events = [
                {
                    "seq": 1,
                    "t": 0,
                    "source": "import",
                    "message": "importing 2 selected",
                    "level": "info",
                },
                {"seq": 2, "t": 0, "source": "import", "message": "added a.gif", "level": "info"},
            ]
        elif seq["n"] == 2:
            seq["n"] = 3
            events = [
                {
                    "seq": 3,
                    "t": 0,
                    "source": "ocr",
                    "message": "reading text: a.gif",
                    "level": "info",
                }
            ]
        else:
            events = []
        route.fulfill(
            content_type="application/json",
            body=json.dumps({"events": events, "cursor": seq["n"]}),
        )

    page.route("**/api/log*", handler)
    page.goto(server)
    page.keyboard.press("Backquote")
    page.locator("#console").wait_for(state="visible", timeout=5_000)
    # The new line arrives on a later poll, at the tail, exactly once.
    page.wait_for_function(
        "document.querySelectorAll('#consolelog .console-line').length === 3",
        timeout=5_000,
    )
    lines = page.locator("#consolelog .console-line").all_inner_texts()
    assert "reading text: a.gif" in lines[-1]
    assert sum("reading text" in ln for ln in lines) == 1, "a cursor tail must not replay"
    assert page.errors == []


def test_reread_text_is_reachable_and_runs(page, server):
    """Re-applying an OCR improvement to existing GIFs: the describe button is
    Claude-only and never touches OCR, so this free re-read lives in the
    library panel's maintenance row. The container has tesseract, so the button
    is enabled here."""
    page.click("#librarybtn")
    page.locator("#library").wait_for(state="visible", timeout=5_000)
    btn = page.locator("#libreocr")
    assert btn.is_visible()
    assert btn.is_enabled(), "an engine is present in the container, so it should be live"
    btn.click()
    # It closes the panel and confirms with a count.
    page.locator("#library").wait_for(state="hidden", timeout=5_000)
    page.locator("#toast", has_text="re-reading text").wait_for(state="visible", timeout=5_000)
    assert page.errors == []


def test_describe_offers_undo_when_it_changes_something(page, server):
    """Describe now regenerates and replaces a GIF's metadata, so it can wipe a
    tag added by hand. The safety net is a per-card undo that appears only when
    the describe actually changed the description or tags, and restores the
    pre-describe values.

    The whole flow is stubbed (describe needs an API key and the network): the
    gif list returns the old values, then the new ones after describe lands,
    and the undo PATCH is captured to prove it restores the snapshot.
    """
    state = {"described": False, "restored": False, "patch": None, "polls": 0}

    def gif(desc, tags):
        return {
            "id": 1,
            "filename": "clip.gif",
            "url": "/gifs/clip.gif",
            "title": "",
            "width": 64,
            "height": 48,
            "bytes": 100,
            "added_at": 0,
            "copies": 0,
            "ocr_text": "",
            "description": desc,
            "tags": tags,
            "source_url": "",
            "ocr_at": 0,
            "enriched_at": 0,
            "sha256": "",
            "phash": "",
        }

    def gifs_route(route):
        if state["restored"]:
            g = gif("old description", ["kept"])
        elif state["described"]:
            # Description replaced, tags merged (the hand tag "kept" survives),
            # which is what a real describe does.
            g = gif("a fresh machine description", ["kept", "fresh"])
        else:
            g = gif("old description", ["kept"])
        route.fulfill(
            content_type="application/json",
            body=json.dumps({"gifs": [g], "tags": [], "root": "/x"}),
        )

    def jobs_route(route):
        jobs = []
        active = 0
        if state["described"]:
            state["polls"] += 1
            status = "running" if state["polls"] == 1 else "done"
            jobs = [
                {
                    "id": 9,
                    "kind": "describe",
                    "label": "clip.gif",
                    "status": status,
                    "detail": "",
                    "done": 0,
                    "total": 0,
                    "created_at": 0,
                }
            ]
            active = 1 if status == "running" else 0
        route.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "jobs": jobs,
                    "active": active,
                    "capabilities": {"ocr": True, "enrich": True, "read_only": False},
                }
            ),
        )

    def enrich_route(route):
        state["described"] = True
        route.fulfill(content_type="application/json", body=json.dumps({"ok": True}))

    def patch_route(route):
        state["patch"] = route.request.post_data
        state["restored"] = True
        route.fulfill(
            content_type="application/json", body=json.dumps(gif("old description", ["kept"]))
        )

    # Order matters: Playwright tries the most recently added route first, and
    # the list fetch carries a query string (`/api/gifs?sort=...`), so the glob
    # has to allow for it. `*` never crosses `/`, so `/api/gifs*` cannot swallow
    # `/api/gifs/1`.
    # The stub GIF has no real file behind it; serve a 1x1 so the <img> does
    # not 404 into the console-error check.
    pixel = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    page.route("**/gifs/clip.gif", lambda r: r.fulfill(content_type="image/gif", body=pixel))
    page.route("**/api/gifs*", gifs_route)
    page.route("**/api/jobs*", jobs_route)
    page.route("**/api/gifs/1", patch_route)
    page.route("**/api/gifs/1/enrich", enrich_route)
    page.goto(server)

    card = page.locator(".card").first
    card.wait_for(state="visible", timeout=5_000)
    # No undo before a describe.
    assert card.locator(".undo").is_hidden()

    card.locator(".describe").click()
    # First the grid reloads with the new machine values (the describe landed),
    page.wait_for_function(
        "document.querySelector('.chips')?.textContent.includes('fresh')", timeout=8_000
    )
    # then the undo is offered because those differ from the snapshot.
    page.wait_for_function(
        "document.querySelector('.card .undo') && !document.querySelector('.card .undo').hidden",
        timeout=5_000,
    )

    page.locator(".card .undo").click()
    page.locator("#toast", has_text="describe undone").wait_for(state="visible", timeout=5_000)
    # The undo PATCHed back exactly the pre-describe snapshot.
    assert state["patch"] and "old description" in state["patch"] and "kept" in state["patch"]
    assert page.errors == []


def test_ocr_text_has_a_per_entry_rerun_and_delete_menu(page, server):
    """Clicking a GIF's burned-in text opens a small re-run / delete menu, the
    way the undo icon works. Delete clears the text (and the affordance flips to
    "read text"); re-run reads it again. Both are real requests, captured here.
    """
    hits = {"rerun": 0, "delete": 0}
    ocr_text = {"v": "HELLO WORLD"}

    def gif():
        return {
            "id": 1,
            "filename": "h.gif",
            "url": "/gifs/h.gif",
            "title": "",
            "width": 64,
            "height": 48,
            "bytes": 100,
            "added_at": 0,
            "copies": 0,
            "ocr_text": ocr_text["v"],
            "description": "",
            "tags": [],
            "source_url": "",
            "ocr_at": 1,
            "enriched_at": 0,
            "sha256": "",
            "phash": "",
        }

    page.route(
        "**/api/gifs*",
        lambda r: r.fulfill(
            content_type="application/json",
            body=json.dumps({"gifs": [gif()], "tags": [], "root": "/x"}),
        ),
    )
    page.route(
        "**/api/jobs*",
        lambda r: r.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "jobs": [],
                    "active": 0,
                    "capabilities": {"ocr": True, "enrich": False, "read_only": False},
                }
            ),
        ),
    )

    def del_ocr(route):
        hits["delete"] += 1
        ocr_text["v"] = ""  # the reload will now show the empty affordance
        route.fulfill(content_type="application/json", body=json.dumps({"ok": True}))

    def run_ocr(route):
        hits["rerun"] += 1
        route.fulfill(content_type="application/json", body=json.dumps({"ok": True}))

    # DELETE and POST hit the same path; split by method.
    def ocr_route(route):
        (del_ocr if route.request.method == "DELETE" else run_ocr)(route)

    pixel = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    page.route("**/gifs/h.gif", lambda r: r.fulfill(content_type="image/gif", body=pixel))
    page.route("**/api/gifs/1/ocr", ocr_route)
    page.goto(server)

    quote = page.locator(".card .quote")
    quote.wait_for(state="visible", timeout=5_000)
    assert "HELLO" in quote.inner_text()
    # Menu hidden until the text is clicked.
    assert page.locator(".ocrmenu").is_hidden()
    quote.click()
    page.locator(".ocrrerun").wait_for(state="visible", timeout=5_000)

    # Delete clears the text; the affordance becomes "read text" and delete goes.
    page.locator(".ocrdelete").click()
    page.wait_for_function(
        "document.querySelector('.card .quote')?.textContent.includes('read text')", timeout=5_000
    )
    assert hits["delete"] == 1
    assert page.locator(".ocrdelete").is_hidden(), "nothing to delete once empty"

    # Re-run from the empty affordance fires the read.
    page.locator(".card .quote").click()
    page.locator(".ocrrerun").click()
    page.locator("#toast", has_text="re-reading text").wait_for(state="visible", timeout=5_000)
    assert hits["rerun"] == 1
    assert page.errors == []


def test_the_model_picker_populates_and_persists(page, server):
    """The describe model picker: live-loaded from /api/models, remembered per
    browser, and the default is not pinned so it can keep tracking the server."""
    page.route(
        "**/api/jobs*",
        lambda r: r.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "jobs": [],
                    "active": 0,
                    "capabilities": {"ocr": True, "enrich": True, "read_only": False},
                }
            ),
        ),
    )
    page.route(
        "**/api/models",
        lambda r: r.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "default": "claude-sonnet-5",
                    "models": [
                        {"id": "claude-sonnet-5", "name": "Claude Sonnet 5"},
                        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5"},
                    ],
                }
            ),
        ),
    )
    page.goto(server)
    page.click("#librarybtn")
    picker = page.locator("#libmodel")
    picker.wait_for(state="visible", timeout=5_000)
    # Populated, default marked and selected, no localStorage pin yet.
    assert picker.locator("option").count() == 2
    assert "default" in picker.locator("option", has_text="Sonnet").inner_text()
    assert page.evaluate("localStorage.getItem('gifhole-model')") is None

    picker.select_option("claude-haiku-4-5")
    assert page.evaluate("localStorage.getItem('gifhole-model')") == "claude-haiku-4-5"
    # Choosing the default again clears the pin rather than freezing it.
    picker.select_option("claude-sonnet-5")
    assert page.evaluate("localStorage.getItem('gifhole-model')") is None
    assert page.errors == []


def test_possible_dupes_prompt_shows_and_opens_review(page, server):
    """The ambient 'possible dupes: N' prompt by the Library button, from the
    poll, and clicking it opens the duplicates review."""
    page.route(
        "**/api/jobs*",
        lambda r: r.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "jobs": [],
                    "active": 0,
                    "duplicates": 2,
                    "capabilities": {"ocr": True, "enrich": False, "read_only": False},
                }
            ),
        ),
    )
    page.route(
        "**/api/duplicates",
        lambda r: r.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "groups": [
                        [
                            {
                                "id": 1,
                                "filename": "a.gif",
                                "url": "/gifs/a.gif",
                                "title": "",
                                "width": 4,
                                "height": 4,
                                "bytes": 1,
                                "added_at": 0,
                                "copies": 0,
                                "ocr_text": "",
                                "description": "",
                                "tags": [],
                                "source_url": "",
                                "ocr_at": 0,
                                "enriched_at": 0,
                                "sha256": "",
                                "phash": "",
                            },
                            {
                                "id": 2,
                                "filename": "b.gif",
                                "url": "/gifs/b.gif",
                                "title": "",
                                "width": 4,
                                "height": 4,
                                "bytes": 1,
                                "added_at": 0,
                                "copies": 0,
                                "ocr_text": "",
                                "description": "",
                                "tags": [],
                                "source_url": "",
                                "ocr_at": 0,
                                "enriched_at": 0,
                                "sha256": "",
                                "phash": "",
                            },
                        ]
                    ]
                }
            ),
        ),
    )
    pixel = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    page.route("**/gifs/*.gif", lambda r: r.fulfill(content_type="image/gif", body=pixel))
    page.goto(server)

    alert = page.locator("#dupealert")
    alert.wait_for(state="visible", timeout=5_000)
    assert "possible dupes: 2" in alert.inner_text()
    assert alert.evaluate("el => el.previousElementSibling.id") == "librarybtn"
    alert.click()
    page.locator("#dupes").wait_for(state="visible", timeout=5_000)
    assert page.errors == []


def test_bookmarklet_picker_selects_only_the_hero_and_disarms(page, server):
    """The bookmarklet lands on one GIF's page, which hands us the main GIF plus
    its size variants. The picker pre-selects only the first (the hero), imports
    the rest but unticked, and disarms Import / select-none when nothing is on."""
    page.route(
        "**/api/fetch/discover",
        lambda r: r.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "kind": "page",
                    "candidates": [
                        {"url": "https://x.test/hero.gif", "kind": "gif", "title": "hero"},
                        {"url": "https://x.test/small.gif", "kind": "gif", "title": "s"},
                        {"url": "https://x.test/tiny.gif", "kind": "gif", "title": "t"},
                    ],
                }
            ),
        ),
    )
    pixel = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    page.route("**/*.gif", lambda r: r.fulfill(content_type="image/gif", body=pixel))
    page.route("**/api/preview*", lambda r: r.fulfill(content_type="image/gif", body=pixel))

    import urllib.parse

    frag = urllib.parse.quote(json.dumps({"page": "https://x.test/gif", "urls": []}))
    page.goto(f"{server}/#add={frag}")

    page.locator("#picker").wait_for(state="visible", timeout=8_000)
    page.wait_for_function(
        "document.querySelectorAll('#pickgrid .pick').length === 3", timeout=5_000
    )
    # Only the hero (first) is ticked.
    checked = page.eval_on_selector_all(
        "#pickgrid input[type=checkbox]", "els => els.map(e => e.checked)"
    )
    assert checked == [True, False, False], checked
    assert "1 of 3 selected" in page.locator("#pickcount").inner_text()
    assert not page.locator("#pickgo").is_disabled()  # armed with the hero on
    assert page.locator("#pickall").is_enabled()  # can still select the rest

    # Deselect everything -> Import and select-none disarm.
    page.click("#picknone")
    assert "0 of 3" in page.locator("#pickcount").inner_text()
    assert page.locator("#pickgo").is_disabled()
    assert page.locator("#picknone").is_disabled()
    assert page.errors == []

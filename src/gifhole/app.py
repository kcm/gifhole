"""FastAPI app serving the library UI and its JSON API."""

from __future__ import annotations

import logging
import os
import secrets
import shutil
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from gifhole import __version__, clipboard, fetch, ocr
from gifhole.jobs import JobQueue
from gifhole.store import Store, split_tags

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _bearer(header: str) -> str:
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def default_root() -> Path:
    return Path(os.environ.get("GIFHOLE_ROOT", Path.home() / ".gifhole")).expanduser()


def display_path(path: Path) -> str:
    """Abbreviate the home directory, so the UI reads as a stable location."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


COOKIE = "gifhole_token"

# Anything that cannot change the library. Everything else is a write, which
# is a deliberately blunt rule: classifying route by route means every new
# route is a chance to forget, and forgetting defaults to letting a reader
# write.
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Reads that are not really reads. /api/preview is a GET, but it makes the
# server fetch a URL chosen by the caller, so leaving it open would be an open
# proxy on someone else's bandwidth and IP. It exists only to preview import
# candidates, which is a writer's flow, so it is treated as a write.
WRITER_ONLY_PATHS = ("/api/preview",)


def needs_a_writer(method: str, path: str) -> bool:
    return method not in READ_ONLY_METHODS or path.startswith(WRITER_ONLY_PATHS)


def configured_token(explicit: str | None = None) -> str:
    return explicit or os.environ.get("GIFHOLE_TOKEN", "")


def configured_read_token(explicit: str | None = None) -> str:
    return explicit or os.environ.get("GIFHOLE_READ_TOKEN", "")


def configured_public_reads(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("GIFHOLE_PUBLIC_READS", "").strip().lower() in {"1", "true", "yes", "on"}


def create_app(
    root: Path | None = None,
    *,
    auto_ocr: bool = True,
    token: str | None = None,
    read_token: str | None = None,
    public_reads: bool | None = None,
) -> FastAPI:
    token = configured_token(token)
    read_token = configured_read_token(read_token)
    public_reads = configured_public_reads(public_reads)
    # Asking for access control and not getting it must stop the process, not
    # log about it. Both of these once warned and carried on serving
    # everything, which is the exact shape of a fail-open: the operator
    # believed they had restricted access, and the only evidence otherwise was
    # a line in a log nobody reads.
    if read_token and not token:
        raise ValueError(
            "a read token needs a write token as well, or writes stay open to "
            "everyone. Set GIFHOLE_TOKEN (or --token)."
        )
    if public_reads and not token:
        raise ValueError(
            "public reads needs a write token as well, or writes are public too. "
            "Set GIFHOLE_TOKEN (or --token)."
        )
    if public_reads and read_token:
        log.warning("GIFHOLE_READ_TOKEN is redundant while reads are public")
    store = Store(root or default_root())
    store.rescan()
    jobs = JobQueue()

    # Preview/import staging. Cleared on start so it can't grow without bound.
    staging_dir = store.root / ".staging"
    shutil.rmtree(staging_dir, ignore_errors=True)

    # No docs, and no schema either. The interactive docs were already off, but
    # openapi.json stayed on and would have been served to anyone under
    # --public-reads, handing out a map of every write route. There is no
    # audience for it here: the API has exactly one client, which ships with it.
    app = FastAPI(title="gifhole", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.jobs = jobs

    # Registered after the cross-site check, so it runs before it: an
    # unauthenticated request should be refused for that reason, not sorted
    # into CSRF categories first.
    @app.middleware("http")
    async def require_token(request, call_next):
        """Gate everything behind a shared token, when one is configured.

        Off by default, so a loopback install behaves exactly as before. When
        set, it covers `/gifs/*` as well as the API: those files are the data,
        and protecting the API while serving the GIFs to anyone would be
        security theatre.

        Three ways to present it. A `Authorization: Bearer` header for API
        clients; a cookie, which is what makes `<img src>` work at all, since a
        tag cannot carry a header; and `?token=` once in the URL bar, which
        sets that cookie. Without the cookie route the UI could authenticate
        its fetches and still show a wall of broken images.
        """
        if not token:
            return await call_next(request)

        offered = (
            _bearer(request.headers.get("authorization", ""))
            or request.cookies.get(COOKIE, "")
            or request.query_params.get("token", "")
        )
        # compare_digest, not ==, so a wrong guess takes the same time to
        # reject however much of it was right.
        writer = bool(offered) and secrets.compare_digest(offered, token)
        reader = bool(offered and read_token) and secrets.compare_digest(offered, read_token)
        writer_only = needs_a_writer(request.method, request.url.path)

        if not writer:
            if writer_only:
                # A reader who reached for something only a writer can do gets
                # 403; a stranger gets 401, because their answer is different:
                # one needs a better token, the other needs any token.
                status, detail = (
                    (403, "that token can look, not touch")
                    if reader
                    else (401, "a token is required for that")
                )
                if not (reader or public_reads):
                    detail = "a token is required; add ?token=... once, or an Authorization header"
                return JSONResponse({"detail": detail}, status_code=status)
            if not (reader or public_reads):
                return JSONResponse(
                    {
                        "detail": "a token is required; add ?token=... once, "
                        "or an Authorization header"
                    },
                    status_code=401,
                )
        # Recorded so a route can report it; the UI hides what it cannot do
        # rather than offering buttons that come back 403.
        request.state.read_only = not writer

        response = await call_next(request)
        if request.query_params.get("token"):
            # Remember it, so images and later requests carry it. HttpOnly
            # keeps it away from any script that manages to run on the page.
            response.set_cookie(
                COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 365
            )
        return response

    @app.middleware("http")
    async def refuse_cross_site_writes(request, call_next):
        """Block cross-site requests that change state.

        There is no auth here, and several mutating routes are CORS-"simple"
        (a bodyless POST, a multipart upload), so any page the user visits
        could auto-submit a form at 127.0.0.1 and, for example, drive billable
        Claude calls via /enrich. Browsers label the origin for us; non-browser
        clients send neither header and are unaffected.
        """
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            site = request.headers.get("sec-fetch-site")
            if site and site not in ("same-origin", "none"):
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    def queue_ocr(gif_id: int, filename: str) -> None:
        """Read burned-in text in the background; failures are never fatal."""
        if not auto_ocr or not ocr.available():
            return

        def run(job):
            result = ocr.read_gif_text(store.gifs_dir / filename)
            if not result.available:
                # Do NOT call set_ocr here. It stamps ocr_at, which would mark
                # this GIF as read and exclude it from needing_ocr() forever,
                # reporting a green job and "no text found" for what was a
                # failure. Raising records the job as failed and leaves it
                # eligible for a later Rescan.
                raise RuntimeError(f"OCR failed: {result.reason or 'unknown error'}")
            store.set_ocr(gif_id, result.text)
            return result.text or "no text found"

        jobs.submit("ocr", filename, run)

    # Backfill whatever was already sitting in the folder. Without this, GIFs
    # present before first launch stay unread until someone hits Rescan.
    for existing in store.needing_ocr():
        queue_ocr(existing.id, existing.filename)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/gifs/{filename}")
    def gif_file(filename: str) -> FileResponse:
        path = (store.gifs_dir / filename).resolve()
        if not path.is_file() or store.gifs_dir.resolve() not in path.parents:
            raise HTTPException(404, "no such gif")
        return FileResponse(path, media_type="image/gif")

    @app.get("/api/gifs")
    def list_gifs(q: str = "", sort: str = "added") -> JSONResponse:
        gifs = store.list_gifs(q, sort)
        return JSONResponse(
            {
                "gifs": [g.as_dict() for g in gifs],
                "tags": [{"tag": t, "count": c} for t, c in store.all_tags()],
                # Absolute for the alt-click path copy; abbreviated for display.
                "root": str(store.gifs_dir),
                "root_display": display_path(store.gifs_dir),
            }
        )

    @app.post("/api/gifs")
    async def upload(file: UploadFile, tags: str = Form(""), force: str = Form("")) -> JSONResponse:
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        name = file.filename or "gif.gif"

        # Duplicates are reported, not rejected. Only the user knows whether a
        # near match is the same GIF or a different cut of the same scene, so
        # this answers 200-with-duplicates and waits to be told again.
        if not force:
            matches = _duplicates_of(data, name)
            if matches:
                return JSONResponse({"duplicate": True, "filename": name, "matches": matches})

        try:
            gif = store.add_bytes(name, data, tags=tags)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        queue_ocr(gif.id, gif.filename)
        return JSONResponse(gif.as_dict(), status_code=201)

    def _duplicates_of(data: bytes, name: str) -> list[dict]:
        """Compare against the library without leaving the candidate on disk.

        The perceptual hash needs a real file to read frames from, so the bytes
        are staged and removed again; adding first and rolling back would be
        worse, since a crash would leave the duplicate in the library.
        """
        staging_dir.mkdir(parents=True, exist_ok=True)
        probe = staging_dir / f"probe-{abs(hash(name))}.gif"
        try:
            probe.write_bytes(data)
            found = store.find_duplicates(data, probe)
        except OSError:
            found = store.find_duplicates(data)
        finally:
            probe.unlink(missing_ok=True)
        return [{**gif.as_dict(), "match": kind} for gif, kind in found]

    @app.get("/api/library")
    def library_stats() -> JSONResponse:
        """Counts for the library panel, including what each scope would cover."""
        return JSONResponse(
            {"stats": store.stats(), "scopes": list(store.SCOPES), "version": __version__}
        )

    @app.get("/api/duplicates")
    def list_duplicates() -> JSONResponse:
        """Duplicates already in the library, for a collection built before this."""
        store.backfill_hashes()
        groups = store.duplicate_groups()
        return JSONResponse({"groups": [[g.as_dict() for g in group] for group in groups]})

    @app.patch("/api/gifs/{gif_id}")
    async def edit(gif_id: int, payload: dict) -> JSONResponse:
        gif = store.update(
            gif_id,
            title=payload.get("title"),
            tags=payload.get("tags"),
            description=payload.get("description"),
        )
        if gif is None:
            raise HTTPException(404, "no such gif")
        return JSONResponse(gif.as_dict())

    @app.post("/api/gifs/{gif_id}/copied")
    def copied(gif_id: int) -> JSONResponse:
        if store.get(gif_id) is None:
            raise HTTPException(404, "no such gif")
        store.bump_copies(gif_id)
        return JSONResponse({"ok": True})

    @app.delete("/api/gifs/{gif_id}")
    def delete(gif_id: int) -> JSONResponse:
        """Moves the file to .trash; nothing is erased from disk."""
        trashed = store.remove(gif_id)
        if trashed is None:
            raise HTTPException(404, "no such gif")
        return JSONResponse({"ok": True, "trash": str(store.trash_dir), "trashed": [trashed]})

    @app.post("/api/gifs/delete")
    def delete_many(payload: dict) -> JSONResponse:
        """Trash a batch in one request, so removing 40 GIFs isn't 40 round trips."""
        ids = [i for i in (payload.get("ids") or []) if isinstance(i, int)]
        trashed = [name for i in ids if (name := store.remove(i)) is not None]
        return JSONResponse({"ok": True, "removed": len(trashed), "trashed": trashed})

    @app.post("/api/gifs/tag")
    def tag_many(payload: dict) -> JSONResponse:
        """Tag a batch in one request.

        Filing a scraped thread is the case this exists for: doing it per GIF
        would be one round trip each, and the point of a bulk action is that it
        costs about the same as tagging one.
        """
        ids = [i for i in (payload.get("ids") or []) if isinstance(i, int)]
        add = split_tags(payload.get("add") or "")
        remove = split_tags(payload.get("remove") or "")
        if not ids or not (add or remove):
            raise HTTPException(400, "need ids and at least one tag")
        changed = store.retag(ids, add, remove)
        return JSONResponse({"ok": True, "changed": len(changed), "asked": len(ids)})

    @app.post("/api/gifs/clear")
    def clear_all(payload: dict) -> JSONResponse:
        """Empty the library into .trash.

        Guarded by an explicit confirm field rather than the method alone: this
        is one request away from clearing everything, and a stray call should
        bounce instead of being obeyed. Nothing is erased; .trash still has it.
        """
        if payload.get("confirm") != "clear":
            raise HTTPException(400, "clearing the library needs confirm=clear")
        trashed = store.clear_library()
        return JSONResponse({"ok": True, "removed": len(trashed), "trashed": trashed})

    # -- the trash -----------------------------------------------------------

    @app.get("/api/trash")
    def list_trash() -> JSONResponse:
        return JSONResponse({"entries": store.trash_entries(), "dir": str(store.trash_dir)})

    @app.post("/api/trash/restore")
    def restore_trash(payload: dict) -> JSONResponse:
        """Put trashed GIFs back. Also what the undo after a delete calls."""
        names = [n for n in (payload.get("names") or []) if isinstance(n, str)]
        restored, missing = [], []
        for name in names:
            try:
                gif = store.restore(name)
            except (FileNotFoundError, OSError):
                missing.append(name)
                continue
            restored.append(gif.as_dict())
            queue_ocr(gif.id, gif.filename)
        return JSONResponse({"ok": True, "restored": restored, "missing": missing})

    @app.post("/api/trash/purge")
    def purge_trash(payload: dict) -> JSONResponse:
        """Delete trashed files for good.

        The only route in the app that destroys anything, so it will not act on
        a bare request: either name the entries, or ask for `all` outright.
        """
        names = [n for n in (payload.get("names") or []) if isinstance(n, str)]
        if payload.get("all") is True:
            return JSONResponse({"ok": True, "purged": store.empty_trash()})
        if not names:
            raise HTTPException(400, "name what to purge, or pass all=true")
        purged = 0
        for name in names:
            try:
                store.purge(name)
            except (FileNotFoundError, OSError):
                continue
            purged += 1
        return JSONResponse({"ok": True, "purged": purged})

    @app.post("/api/rescan")
    def rescan() -> JSONResponse:
        result = store.rescan()
        for gif in store.needing_ocr():
            queue_ocr(gif.id, gif.filename)
        return JSONResponse(result)

    # -- downloading ---------------------------------------------------------

    @app.post("/api/fetch/discover")
    def discover_url(payload: dict) -> JSONResponse:
        """List what a URL offers, without downloading any of it.

        A page yields every candidate so the UI can show a selection screen;
        a direct media link yields one, and the UI imports it straight away.
        """
        url = (payload.get("url") or "").strip()
        if not fetch.looks_like_url(url):
            raise HTTPException(400, "that does not look like an http(s) URL")
        try:
            kind, candidates = fetch.discover(url)
        except fetch.FetchError as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse(
            {
                "kind": kind,
                "candidates": [
                    {"url": c.url, "kind": c.kind, "title": c.title} for c in candidates
                ],
            }
        )

    @app.get("/api/preview")
    def preview(url: str) -> Response:
        """Proxy a candidate for the selection screen.

        Reddit's preview CDNs refuse cross-origin browser loads, so previewing
        from the original URL shows a broken image for most of a thread. The
        bytes are staged, so importing afterwards doesn't fetch them again.
        """
        try:
            fetch.ensure_public_http_url(url)
            data = fetch.fetch_bytes_cached(url, staging_dir)
        except fetch.FetchError as exc:
            raise HTTPException(502, str(exc)) from exc
        media = "video/mp4" if fetch._classify(url) == "video" else "image/gif"
        return Response(content=data, media_type=media)

    @app.post("/api/fetch/import")
    def import_selected(payload: dict) -> JSONResponse:
        """Import exactly the candidates the user ticked."""
        urls = [u for u in (payload.get("urls") or []) if isinstance(u, str)]
        titles = payload.get("titles") or {}
        if not urls:
            raise HTTPException(400, "nothing selected")

        def run(job):
            job.total = len(urls)
            report = fetch.import_urls(store, urls, staging_dir, titles)
            job.done = len(report.added)
            # Previewing a large thread can stage hundreds of MB. The picker is
            # closed by now and nothing else reads these, so drop them rather
            # than letting staging grow for the rest of the session.
            shutil.rmtree(staging_dir, ignore_errors=True)
            for name in report.added:
                gif = next((g for g in store.list_gifs() if g.filename == name), None)
                if gif:
                    queue_ocr(gif.id, gif.filename)
            if not report.added:
                first = report.skipped[0][1] if report.skipped else "nothing usable found"
                raise fetch.FetchError(first)
            return f"added {len(report.added)}, skipped {len(report.skipped)}"

        job = jobs.submit("import", f"{len(urls)} selected", run)
        return JSONResponse(job.as_dict(), status_code=202)

    # -- metadata ------------------------------------------------------------

    @app.post("/api/gifs/{gif_id}/ocr")
    def run_ocr(gif_id: int) -> JSONResponse:
        gif = store.get(gif_id)
        if gif is None:
            raise HTTPException(404, "no such gif")
        if not ocr.available():
            raise HTTPException(503, "no OCR engine available; install tesseract")
        queue_ocr(gif.id, gif.filename)
        return JSONResponse({"ok": True})

    @app.post("/api/gifs/{gif_id}/clipboard")
    def copy_to_clipboard(gif_id: int) -> JSONResponse:
        """Put the GIF on the pasteboard as a file, so pastes stay animated.

        A browser can only offer a still PNG. Handing over the file lets
        Discord, Slack and friends upload the real GIF.
        """
        gif = store.get(gif_id)
        if gif is None:
            raise HTTPException(404, "no such gif")
        if not clipboard.available():
            raise HTTPException(503, "the file clipboard needs macOS")
        try:
            clipboard.copy_file(store.gifs_dir / gif.filename)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(500, str(exc)) from exc
        store.bump_copies(gif_id)
        return JSONResponse({"ok": True, "filename": gif.filename})

    # `enrich.available()` stays permissive on purpose: it cannot see an
    # `ant auth login` profile, so refusing on a missing env var would wrongly
    # disable the feature for people who have one. The cost of that is a batch
    # discovering the problem per GIF. So the first auth failure latches here,
    # and the rest of the queue fails instantly instead of making 99 more calls
    # that cannot succeed. Any later success clears it.
    auth_block: dict[str, str | None] = {"why": None}

    def queue_enrich(gif) -> None:
        """Describe one GIF in the background, tagged from the live vocabulary.

        The vocabulary is read when the job runs, not when it is queued, so a
        batch of 100 gets steadily more consistent: tags the earlier GIFs
        introduced are on offer to the later ones.
        """
        from gifhole import enrich

        def run(job):
            if auth_block["why"]:
                raise enrich.EnrichError(auth_block["why"])
            vocabulary = [tag for tag, _ in store.all_tags()]
            try:
                result = enrich.describe_gif(store.gifs_dir / gif.filename, vocabulary=vocabulary)
            except enrich.EnrichError as exc:
                if any(w in str(exc).lower() for w in ("authentication", "api_key", "api key")):
                    auth_block["why"] = (
                        "no API key. Set ANTHROPIC_API_KEY or run `ant auth login`, then try again"
                    )
                raise
            auth_block["why"] = None
            before = set(store.get(gif.id).tags if store.get(gif.id) else [])
            store.set_enrichment(gif.id, result["description"], " ".join(result["tags"]))
            after = store.get(gif.id)
            added = [t for t in (after.tags if after else []) if t not in before]
            # Say what changed. A constrained vocabulary often picks tags the
            # GIF already had, which is correct but looks like nothing
            # happened unless the job says so.
            note = f"+{len(added)} tags: {' '.join(added)}" if added else "no new tags"
            return f"{note} · {result['description'][:60]}"

        jobs.submit("describe", gif.filename, run)

    @app.post("/api/gifs/{gif_id}/enrich")
    def run_enrich(gif_id: int) -> JSONResponse:
        """Describe a GIF with Claude. Opt-in, per GIF, costs money."""
        gif = store.get(gif_id)
        if gif is None:
            raise HTTPException(404, "no such gif")
        from gifhole import enrich

        ok, why = enrich.available()
        if not ok:
            raise HTTPException(503, why)
        queue_enrich(gif)
        return JSONResponse({"ok": True, "queued": 1}, status_code=202)

    @app.post("/api/gifs/describe")
    def describe_many(payload: dict) -> JSONResponse:
        """Describe a batch. Every GIF is a billable API call, so the caller
        has to name them: there is no "describe everything" shortcut here."""
        from gifhole import enrich

        ok, why = enrich.available()
        if not ok:
            raise HTTPException(503, why)
        scope = payload.get("scope")
        if scope:
            # A library-wide run. The scope already says what to include, so it
            # is not filtered again by enriched_at: "all" means all.
            try:
                gifs = store.in_scope(scope)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        else:
            ids = [i for i in (payload.get("ids") or []) if isinstance(i, int)]
            if not ids:
                raise HTTPException(400, "nothing selected")
            gifs = [g for g in (store.get(i) for i in ids) if g is not None]
            # Skipping already-described GIFs by default makes re-running a
            # batch cheap instead of paying twice for the same answer.
            if not payload.get("redo"):
                gifs = [g for g in gifs if not g.enriched_at]
        for gif in gifs:
            queue_enrich(gif)
        asked = len(store.list_gifs()) if scope else len(payload.get("ids") or [])
        return JSONResponse(
            {"ok": True, "queued": len(gifs), "skipped": max(0, asked - len(gifs))},
            status_code=202,
        )

    # -- jobs ----------------------------------------------------------------

    @app.post("/api/jobs/cancel")
    def cancel_jobs(payload: dict | None = None) -> JSONResponse:
        """Stop what has not started yet.

        The point is a long describe run: 150 queued GIFs is 150 billable calls
        and there was no way to change your mind halfway. The job already
        running still finishes, so this reports what it actually stopped rather
        than implying an instant halt.
        """
        kind = (payload or {}).get("kind")
        stopped = jobs.cancel(kind)
        # A cancelled describe run should not leave the latch set from an
        # unrelated earlier failure.
        auth_block["why"] = None
        return JSONResponse({"ok": True, "cancelled": stopped})

    @app.get("/api/jobs")
    def list_jobs(request: Request) -> JSONResponse:
        from gifhole import enrich

        enrich_ok, enrich_why = enrich.available()
        return JSONResponse(
            {
                "jobs": [j.as_dict() for j in jobs.list_jobs()[:20]],
                "active": jobs.active(),
                "capabilities": {
                    "ocr": ocr.available(),
                    "ocr_engine": ocr.backend(),
                    "enrich": enrich_ok,
                    "enrich_reason": enrich_why,
                    "ffmpeg": fetch.ffmpeg_available(),
                    "file_clipboard": clipboard.available(),
                    "version": __version__,
                    "read_only": getattr(request.state, "read_only", False),
                },
            }
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


# Deliberately no module-level `app = create_app()`. Building one at import time
# created a real library under ~/.gifhole, started a worker thread, and queued
# Vision OCR over the user's actual GIFs the moment anything imported this
# module, including the test suite. Serve it as a factory instead:
#   uvicorn --factory gifhole.app:create_app

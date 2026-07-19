"""FastAPI app serving the library UI and its JSON API."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from gifhole import clipboard, fetch, ocr
from gifhole.jobs import JobQueue
from gifhole.store import Store

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def default_root() -> Path:
    return Path(os.environ.get("GIFHOLE_ROOT", Path.home() / ".gifhole")).expanduser()


def display_path(path: Path) -> str:
    """Abbreviate the home directory, so the UI reads as a stable location."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def create_app(root: Path | None = None, *, auto_ocr: bool = True) -> FastAPI:
    store = Store(root or default_root())
    store.rescan()
    jobs = JobQueue()

    # Preview/import staging. Cleared on start so it can't grow without bound.
    staging_dir = store.root / ".staging"
    shutil.rmtree(staging_dir, ignore_errors=True)

    app = FastAPI(title="gifhole", docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.jobs = jobs

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
        if not auto_ocr or not ocr.vision_available():
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
    async def upload(file: UploadFile, tags: str = Form("")) -> JSONResponse:
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        try:
            gif = store.add_bytes(file.filename or "gif.gif", data, tags=tags)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        queue_ocr(gif.id, gif.filename)
        return JSONResponse(gif.as_dict(), status_code=201)

    @app.patch("/api/gifs/{gif_id}")
    async def edit(gif_id: int, payload: dict) -> JSONResponse:
        gif = store.update(gif_id, title=payload.get("title"), tags=payload.get("tags"))
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
        if not ocr.vision_available():
            raise HTTPException(503, "OCR needs macOS Vision, which is unavailable here")
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

        def run(job):
            result = enrich.describe_gif(store.gifs_dir / gif.filename)
            store.set_enrichment(gif.id, result["description"], " ".join(result["tags"]))
            return result["meme_name"] or result["description"][:60]

        job = jobs.submit("enrich", gif.filename, run)
        return JSONResponse(job.as_dict(), status_code=202)

    # -- jobs ----------------------------------------------------------------

    @app.get("/api/jobs")
    def list_jobs() -> JSONResponse:
        from gifhole import enrich

        enrich_ok, enrich_why = enrich.available()
        return JSONResponse(
            {
                "jobs": [j.as_dict() for j in jobs.list_jobs()[:20]],
                "active": jobs.active(),
                "capabilities": {
                    "ocr": ocr.vision_available(),
                    "enrich": enrich_ok,
                    "enrich_reason": enrich_why,
                    "ffmpeg": fetch.ffmpeg_available(),
                    "file_clipboard": clipboard.available(),
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

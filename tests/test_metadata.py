"""Frames, the job queue, and how OCR text feeds search.

Hermetic: no Vision calls, no network. The OCR path is exercised by injecting
text through the store, which is the contract the rest of the app relies on.
"""

import time

from gifhole.frames import sample_frames, to_png_bytes, upscale_for_ocr
from gifhole.jobs import JobQueue
from tests.conftest import make_animated_gif, make_gif


def test_sample_frames_spreads_across_animation(tmp_path):
    path = tmp_path / "anim.gif"
    path.write_bytes(make_animated_gif(frames=8))
    assert len(sample_frames(path, count=3)) == 3


def test_sample_frames_handles_single_frame_gif(tmp_path):
    path = tmp_path / "still.gif"
    path.write_bytes(make_gif())
    assert len(sample_frames(path, count=3)) == 1


def test_upscale_only_grows_small_frames(tmp_path):
    path = tmp_path / "small.gif"
    path.write_bytes(make_gif(40, 30))
    frame = sample_frames(path)[0]
    assert max(upscale_for_ocr(frame).size) >= 800


def test_to_png_bytes_produces_a_png(tmp_path):
    path = tmp_path / "a.gif"
    path.write_bytes(make_gif())
    assert to_png_bytes(sample_frames(path)[0]).startswith(b"\x89PNG")


def test_ocr_text_is_searchable(store):
    """The payoff: text burned into a GIF is findable without any tagging."""
    gif = store.add_bytes("mystery.gif", make_gif())
    assert store.list_gifs("nope") == []

    store.set_ocr(gif.id, "NOPE NOPE NOPE")
    assert [g.filename for g in store.list_gifs("nope")] == ["mystery.gif"]


def test_description_is_searchable(store):
    gif = store.add_bytes("x.gif", make_gif())
    store.set_enrichment(gif.id, "A cat knocking a glass off a table", "cat clumsy")
    assert [g.filename for g in store.list_gifs("knocking")] == ["x.gif"]
    assert [g.filename for g in store.list_gifs("clumsy")] == ["x.gif"]


def test_enrichment_merges_tags_without_duplicating(store):
    gif = store.add_bytes("y.gif", make_gif(), tags="cat reaction")
    store.set_enrichment(gif.id, "desc", "cat funny")
    assert store.get(gif.id).tags == ["cat", "reaction", "funny"]


def test_source_url_is_recorded(store):
    gif = store.add_bytes("z.gif", make_gif(), source_url="https://example.com/z.gif")
    assert store.get(gif.id).source_url == "https://example.com/z.gif"


def test_needing_ocr_tracks_what_has_been_read(store):
    a = store.add_bytes("a.gif", make_gif())
    store.add_bytes("b.gif", make_gif())
    assert len(store.needing_ocr()) == 2

    store.set_ocr(a.id, "some text")
    assert [g.filename for g in store.needing_ocr()] == ["b.gif"]


def test_migration_adds_columns_to_an_existing_database(tmp_path):
    """An older database must gain the new columns without losing rows."""
    import sqlite3

    from gifhole.store import Store

    db = sqlite3.connect(tmp_path / "gifhole.db")
    db.executescript(
        """CREATE TABLE gifs (
               id INTEGER PRIMARY KEY, filename TEXT NOT NULL UNIQUE,
               title TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
               width INTEGER DEFAULT 0, height INTEGER DEFAULT 0,
               bytes INTEGER DEFAULT 0, added_at REAL DEFAULT 0,
               copies INTEGER DEFAULT 0);
           INSERT INTO gifs (filename, title) VALUES ('old.gif', 'Legacy');"""
    )
    db.commit()
    db.close()

    (tmp_path / "gifs").mkdir()
    (tmp_path / "gifs" / "old.gif").write_bytes(make_gif())

    store = Store(tmp_path)
    survivor = next(g for g in store.list_gifs() if g.filename == "old.gif")
    assert survivor.title == "Legacy"
    assert survivor.ocr_text == ""
    assert survivor.source_url == ""


# -- job queue ---------------------------------------------------------------


def test_job_runs_and_reports_done():
    q = JobQueue()
    seen = []
    q.submit("test", "work", lambda job: seen.append(1) or "finished")
    assert q.wait_idle()
    assert seen == [1]
    assert q.list_jobs()[0].status == "done"
    assert q.list_jobs()[0].detail == "finished"


def test_failing_job_is_recorded_not_swallowed():
    q = JobQueue()

    def boom(job):
        raise RuntimeError("kaboom")

    q.submit("test", "bad", boom)
    assert q.wait_idle()
    job = q.list_jobs()[0]
    assert job.status == "error"
    assert "kaboom" in job.detail


def test_worker_survives_a_failing_job():
    """One bad job must not take the queue down for everything after it."""
    q = JobQueue()

    def boom(job):
        raise RuntimeError("kaboom")

    q.submit("test", "bad", boom)
    q.submit("test", "good", lambda job: "ok")
    assert q.wait_idle()
    assert [j.status for j in q.list_jobs()] == ["done", "error"]


def test_active_count_drops_to_zero():
    q = JobQueue()
    q.submit("test", "slow", lambda job: time.sleep(0.05))
    assert q.wait_idle()
    assert q.active() == 0


def test_failed_ocr_is_recorded_as_a_failure_not_as_empty_text(tmp_path, monkeypatch):
    """A failed read must not stamp ocr_at, or the GIF is never retried."""
    from fastapi.testclient import TestClient

    from gifhole import ocr
    from gifhole.app import create_app

    monkeypatch.setattr(ocr, "vision_available", lambda: True)
    monkeypatch.setattr(
        ocr,
        "read_gif_text",
        lambda *a, **k: ocr.OcrResult("", available=False, reason="decode blew up"),
    )

    app = create_app(tmp_path, auto_ocr=True)
    client = TestClient(app)
    client.post("/api/gifs", files={"file": ("boom.gif", make_gif(), "image/gif")})
    assert app.state.jobs.wait_idle()

    ocr_jobs = [j for j in app.state.jobs.list_jobs() if j.kind == "ocr"]
    assert [j.status for j in ocr_jobs] == ["error"]
    assert "decode blew up" in ocr_jobs[0].detail
    # Still eligible for a retry rather than silently marked as read.
    assert app.state.store.list_gifs()[0].ocr_at == 0
    assert len(app.state.store.needing_ocr()) == 1


# -- constrained tagging -----------------------------------------------------

from gifhole.enrich import MAX_NEW_TAGS, build_schema, merge_result  # noqa: E402


def test_schema_pins_tags_to_the_existing_vocabulary():
    """The whole point: the model cannot return an off-vocabulary known_tag."""
    schema = build_schema(["reaction", "cat", "meme"])
    assert schema["properties"]["known_tags"]["items"]["enum"] == ["cat", "meme", "reaction"]
    assert schema["properties"]["new_tags"]["maxItems"] == MAX_NEW_TAGS


def test_schema_without_a_vocabulary_has_no_empty_enum():
    """An empty enum is invalid JSON Schema and would be rejected outright."""
    known = build_schema([])["properties"]["known_tags"]
    assert "enum" not in known["items"]
    assert known["items"]["type"] == "string"


def test_merge_keeps_vocabulary_tags_and_genuinely_new_ones():
    out = merge_result(
        {
            "description": "a cat knocks a glass off a table",
            "meme_name": "",
            "known_tags": ["cat", "reaction"],
            "new_tags": ["mischief"],
        },
        ["cat", "reaction"],
    )
    assert out["tags"] == ["cat", "reaction", "mischief"]


def test_merge_drops_new_tags_that_duplicate_the_vocabulary():
    """A "new" tag that already exists is not new; it must not appear twice."""
    out = merge_result(
        {"description": "d", "meme_name": "", "known_tags": ["cat"], "new_tags": ["Cat", "cat"]},
        ["cat"],
    )
    assert out["tags"] == ["cat"]


def test_merge_drops_multi_word_tags():
    """Only new_tags is unconstrained, so it is the one place this can arrive."""
    out = merge_result(
        {"description": "d", "meme_name": "", "known_tags": [], "new_tags": ["very annoyed"]},
        [],
    )
    assert out["tags"] == []


def test_meme_name_goes_in_the_description_not_the_tags():
    """Splitting it into tags used to shed junk like "distracted"/"boyfriend"."""
    out = merge_result(
        {
            "description": "a man looks back at another woman",
            "meme_name": "distracted boyfriend",
            "known_tags": ["reaction"],
            "new_tags": [],
        },
        ["reaction"],
    )
    assert out["tags"] == ["reaction"]
    assert out["description"].startswith("distracted boyfriend: ")


def test_meme_name_is_not_repeated_when_already_in_the_description():
    out = merge_result(
        {"description": "This Is Fine: a dog in a fire", "meme_name": "this is fine",
         "known_tags": [], "new_tags": []},
        [],
    )
    assert out["description"] == "This Is Fine: a dog in a fire"


def test_null_meme_name_does_not_crash():
    """The model returns null, not "", when it recognises nothing."""
    out = merge_result(
        {"description": "d", "meme_name": None, "known_tags": [], "new_tags": []}, []
    )
    assert out["meme_name"] == ""
    assert out["description"] == "d"

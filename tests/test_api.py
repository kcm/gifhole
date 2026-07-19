import itertools

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_gif, make_textured_gif


def upload(client, name="test.gif", tags=""):
    return client.post(
        "/api/gifs",
        files={"file": (name, make_gif(), "image/gif")},
        data={"tags": tags},
    )


def test_index_serves_the_ui(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "gifhole" in res.text


def test_upload_then_list(client):
    res = upload(client, "wave.gif", tags="hello greeting")
    assert res.status_code == 201
    assert res.json()["tags"] == ["hello", "greeting"]

    body = client.get("/api/gifs").json()
    assert [g["filename"] for g in body["gifs"]] == ["wave.gif"]
    assert {t["tag"] for t in body["tags"]} == {"hello", "greeting"}


def test_upload_rejects_non_gif(client):
    res = client.post("/api/gifs", files={"file": ("x.gif", b"nope", "image/gif")})
    assert res.status_code == 400


def test_gif_bytes_are_served(client):
    upload(client, "served.gif")
    res = client.get("/gifs/served.gif")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/gif"
    # Both signatures are valid GIF; Pillow emits 87a for simple stills.
    assert res.content.startswith((b"GIF87a", b"GIF89a"))


def test_path_traversal_is_refused(client):
    assert client.get("/gifs/..%2f..%2fgifhole.db").status_code == 404


def test_edit_title_and_tags(client):
    gif_id = upload(client).json()["id"]
    res = client.patch(f"/api/gifs/{gif_id}", json={"title": "Big Wave", "tags": "Ocean, surf"})
    assert res.json()["title"] == "Big Wave"
    assert res.json()["tags"] == ["ocean", "surf"]


def test_copy_counter_increments_and_sorts(client):
    a = upload(client, "a.gif").json()["id"]
    upload(client, "b.gif")
    client.post(f"/api/gifs/{a}/copied")
    client.post(f"/api/gifs/{a}/copied")

    top = client.get("/api/gifs", params={"sort": "copies"}).json()["gifs"][0]
    assert top["filename"] == "a.gif"
    assert top["copies"] == 2


def test_delete_then_missing(client):
    gif_id = upload(client).json()["id"]
    assert client.delete(f"/api/gifs/{gif_id}").json()["ok"] is True
    assert client.get("/api/gifs").json()["gifs"] == []
    assert client.delete(f"/api/gifs/{gif_id}").status_code == 404


def test_unknown_ids_404(client):
    assert client.patch("/api/gifs/999", json={"title": "x"}).status_code == 404
    assert client.post("/api/gifs/999/copied").status_code == 404


# -- cross-site write protection ---------------------------------------------


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({}, True),  # non-browser client sends neither header
        ({"sec-fetch-site": "same-origin"}, True),
        ({"sec-fetch-site": "none"}, True),
        ({"sec-fetch-site": "cross-site"}, False),
        ({"sec-fetch-site": "same-site"}, False),
        ({"origin": "https://evil.example"}, False),
    ],
)
def test_cross_site_writes_are_refused(client, headers, allowed):
    """A visited page must not be able to POST at the local server.

    /enrich takes no body, so it is a CORS-simple request and would otherwise
    be triggerable from any site, spending real API credit.
    """
    res = client.post("/api/rescan", headers=headers)
    assert (res.status_code != 403) is allowed


def test_cross_site_reads_are_unaffected(client):
    assert client.get("/api/gifs", headers={"sec-fetch-site": "cross-site"}).status_code == 200


# -- removal and the trash ---------------------------------------------------


_seq = itertools.count(1)


def _add(client, name="a.gif"):
    """Upload one distinct GIF and return its id.

    Distinct on purpose: uploading the same bytes twice is now reported as a
    duplicate rather than silently creating a second copy, so a helper that
    reused one fixture would stop adding anything after the first call.
    """
    # force: these tests want a GIF in the library, not a duplicate check. Two
    # generated fixtures can land close enough to read as near-duplicates, and
    # then the upload answers with matches and no id.
    res = client.post(
        "/api/gifs",
        files={"file": (name, make_textured_gif(next(_seq)), "image/gif")},
        data={"force": "1"},
    )
    return res.json()["id"]


def test_bulk_delete_trashes_every_id(client):
    ids = [_add(client, f"b{i}.gif") for i in range(3)]
    res = client.post("/api/gifs/delete", json={"ids": ids})
    assert res.json()["removed"] == 3
    assert client.get("/api/gifs").json()["gifs"] == []
    assert len(client.get("/api/trash").json()["entries"]) == 3


def test_bulk_delete_skips_ids_that_are_already_gone(client):
    gif_id = _add(client)
    res = client.post("/api/gifs/delete", json={"ids": [gif_id, 4242]})
    assert res.json()["removed"] == 1


def test_delete_reports_what_it_trashed_so_it_can_be_undone(client):
    gif_id = _add(client, "undome.gif")
    trashed = client.delete(f"/api/gifs/{gif_id}").json()["trashed"]
    assert len(trashed) == 1
    restored = client.post("/api/trash/restore", json={"names": trashed}).json()
    assert [g["filename"] for g in restored["restored"]] == ["undome.gif"]
    assert len(client.get("/api/gifs").json()["gifs"]) == 1


def test_restore_reports_names_it_could_not_find(client):
    res = client.post("/api/trash/restore", json={"names": ["nope-1234.gif"]})
    assert res.json() == {"ok": True, "restored": [], "missing": ["nope-1234.gif"]}


def test_clearing_the_library_needs_the_confirm_field(client):
    _add(client)
    assert client.post("/api/gifs/clear", json={}).status_code == 400
    assert client.post("/api/gifs/clear", json={"confirm": "nope"}).status_code == 400
    assert len(client.get("/api/gifs").json()["gifs"]) == 1


def test_clearing_the_library_keeps_everything_in_the_trash(client):
    ids = [_add(client, f"c{i}.gif") for i in range(3)]
    res = client.post("/api/gifs/clear", json={"confirm": "clear"})
    assert res.json()["removed"] == len(ids)
    assert client.get("/api/gifs").json()["gifs"] == []
    assert len(client.get("/api/trash").json()["entries"]) == 3


def test_purge_will_not_act_without_being_told_what(client):
    """The one destructive route in the app; a bare call must not empty it."""
    _add(client)
    client.post("/api/gifs/clear", json={"confirm": "clear"})
    assert client.post("/api/trash/purge", json={}).status_code == 400
    assert len(client.get("/api/trash").json()["entries"]) == 1


def test_purge_all_empties_the_trash(client):
    _add(client)
    client.post("/api/gifs/clear", json={"confirm": "clear"})
    assert client.post("/api/trash/purge", json={"all": True}).json()["purged"] == 1
    assert client.get("/api/trash").json()["entries"] == []


def test_purge_cannot_reach_outside_the_trash(client):
    gif_id = _add(client, "safe.gif")
    res = client.post("/api/trash/purge", json={"names": ["../gifs/safe.gif"]})
    assert res.json()["purged"] == 0
    assert client.get("/api/gifs").json()["gifs"][0]["id"] == gif_id


# -- bulk tagging ------------------------------------------------------------


def test_bulk_tag_adds_without_replacing_existing_tags(client):
    a = client.post(
        "/api/gifs",
        files={"file": ("a.gif", make_textured_gif(11), "image/gif")},
        data={"tags": "cat"},
    ).json()["id"]
    b = client.post(
        "/api/gifs", files={"file": ("b.gif", make_textured_gif(12), "image/gif")}
    ).json()["id"]
    res = client.post("/api/gifs/tag", json={"ids": [a, b], "add": "reaction meme"})
    assert res.json()["changed"] == 2
    by_name = {g["filename"]: g["tags"] for g in client.get("/api/gifs").json()["gifs"]}
    assert by_name["a.gif"] == ["cat", "reaction", "meme"]
    assert by_name["b.gif"] == ["reaction", "meme"]


def test_bulk_tag_removes(client):
    ids = [
        client.post(
            "/api/gifs",
            files={"file": (f"r{i}.gif", make_textured_gif(20 + i), "image/gif")},
            data={"tags": "todo keep"},
        ).json()["id"]
        for i in range(2)
    ]
    client.post("/api/gifs/tag", json={"ids": ids, "remove": "todo"})
    for gif in client.get("/api/gifs").json()["gifs"]:
        assert gif["tags"] == ["keep"]


def test_bulk_tag_can_add_and_remove_in_one_call(client):
    gif_id = client.post(
        "/api/gifs",
        files={"file": ("s.gif", make_textured_gif(31), "image/gif")},
        data={"tags": "old"},
    ).json()["id"]
    client.post("/api/gifs/tag", json={"ids": [gif_id], "add": "new", "remove": "old"})
    assert client.get("/api/gifs").json()["gifs"][0]["tags"] == ["new"]


def test_bulk_tag_reports_only_what_actually_changed(client):
    """Re-applying a tag every GIF already has must not count as a write."""
    gif_id = client.post(
        "/api/gifs",
        files={"file": ("t.gif", make_textured_gif(32), "image/gif")},
        data={"tags": "same"},
    ).json()["id"]
    res = client.post("/api/gifs/tag", json={"ids": [gif_id], "add": "same"})
    assert res.json() == {"ok": True, "changed": 0, "asked": 1}


def test_bulk_tag_needs_ids_and_tags(client):
    gif_id = client.post(
        "/api/gifs", files={"file": ("u.gif", make_textured_gif(33), "image/gif")}
    ).json()["id"]
    assert client.post("/api/gifs/tag", json={"ids": [], "add": "x"}).status_code == 400
    assert client.post("/api/gifs/tag", json={"ids": [gif_id], "add": "  "}).status_code == 400


def test_bulk_tag_skips_ids_that_are_gone(client):
    gif_id = client.post(
        "/api/gifs", files={"file": ("v.gif", make_textured_gif(34), "image/gif")}
    ).json()["id"]
    res = client.post("/api/gifs/tag", json={"ids": [gif_id, 9999], "add": "here"})
    assert res.json() == {"ok": True, "changed": 1, "asked": 2}


# -- batch describe ----------------------------------------------------------


def test_describe_batch_degrades_when_enrichment_is_unavailable(client, monkeypatch):
    """No key must mean a clear 503, never a half-run batch."""
    from gifhole import enrich

    monkeypatch.setattr(enrich, "available", lambda: (False, "no anthropic package"))
    gif_id = client.post(
        "/api/gifs", files={"file": ("d.gif", make_textured_gif(35), "image/gif")}
    ).json()["id"]
    res = client.post("/api/gifs/describe", json={"ids": [gif_id]})
    assert res.status_code == 503
    assert res.json()["detail"] == "no anthropic package"


def test_describe_batch_needs_a_selection(client, monkeypatch):
    from gifhole import enrich

    monkeypatch.setattr(enrich, "available", lambda: (True, ""))
    assert client.post("/api/gifs/describe", json={"ids": []}).status_code == 400


# -- duplicates on add -------------------------------------------------------


def test_uploading_the_same_file_twice_reports_a_duplicate(client):
    data = make_textured_gif(41)
    first = client.post("/api/gifs", files={"file": ("x.gif", data, "image/gif")})
    assert first.status_code == 201

    again = client.post("/api/gifs", files={"file": ("x.gif", data, "image/gif")})
    body = again.json()
    assert body["duplicate"] is True
    assert body["matches"][0]["match"] == "exact"
    # Nothing was written: reporting a duplicate must not also create one.
    assert len(client.get("/api/gifs").json()["gifs"]) == 1


def test_force_adds_the_duplicate_anyway(client):
    data = make_textured_gif(42)
    client.post("/api/gifs", files={"file": ("y.gif", data, "image/gif")})
    forced = client.post(
        "/api/gifs", files={"file": ("y.gif", data, "image/gif")}, data={"force": "1"}
    )
    assert forced.status_code == 201
    assert len(client.get("/api/gifs").json()["gifs"]) == 2


def test_a_resized_copy_is_reported_as_a_near_duplicate(client):
    client.post(
        "/api/gifs", files={"file": ("big.gif", make_textured_gif(43, 320, 240), "image/gif")}
    )
    res = client.post(
        "/api/gifs", files={"file": ("small.gif", make_textured_gif(43, 160, 120), "image/gif")}
    )
    body = res.json()
    assert body["duplicate"] is True
    assert body["matches"][0]["match"] == "near"


def test_an_unrelated_gif_is_not_reported(client):
    client.post("/api/gifs", files={"file": ("a.gif", make_textured_gif(44), "image/gif")})
    res = client.post("/api/gifs", files={"file": ("b.gif", make_textured_gif(45), "image/gif")})
    assert res.status_code == 201


def test_duplicates_endpoint_groups_what_is_already_there(client):
    data = make_textured_gif(46)
    client.post("/api/gifs", files={"file": ("one.gif", data, "image/gif")})
    client.post("/api/gifs", files={"file": ("two.gif", data, "image/gif")}, data={"force": "1"})
    groups = client.get("/api/duplicates").json()["groups"]
    assert len(groups) == 1
    assert sorted(g["filename"] for g in groups[0]) == ["one.gif", "two.gif"]


def test_description_can_be_edited_by_hand(client):
    gif_id = _add(client, "note.gif")
    res = client.patch(f"/api/gifs/{gif_id}", json={"description": "  a dog gives up  "})
    assert res.json()["description"] == "a dog gives up"


def test_editing_the_description_does_not_mark_it_as_described(client):
    """enriched_at means "Claude has seen this", so a batch describe should
    still pick up a GIF the user merely annotated by hand."""
    gif_id = _add(client, "hand.gif")
    client.patch(f"/api/gifs/{gif_id}", json={"description": "mine"})
    assert client.get("/api/gifs").json()["gifs"][0]["enriched_at"] == 0


def test_a_hand_written_description_is_searchable(client):
    gif_id = _add(client, "find.gif")
    client.patch(f"/api/gifs/{gif_id}", json={"description": "elephant on a trampoline"})
    found = client.get("/api/gifs", params={"q": "trampoline"}).json()["gifs"]
    assert [g["id"] for g in found] == [gif_id]


def test_editing_the_description_leaves_tags_and_title_alone(client):
    gif_id = _add(client, "keep.gif")
    client.patch(f"/api/gifs/{gif_id}", json={"title": "Keeper", "tags": "cat reaction"})
    client.patch(f"/api/gifs/{gif_id}", json={"description": "just the description"})
    gif = client.get("/api/gifs").json()["gifs"][0]
    assert gif["title"] == "Keeper"
    assert gif["tags"] == ["cat", "reaction"]


# -- library panel -----------------------------------------------------------


def test_library_stats_count_each_scope(client):
    a = _add(client, "a.gif")
    b = _add(client, "b.gif")
    _add(client, "c.gif")
    client.patch(f"/api/gifs/{a}", json={"tags": "one"})
    client.patch(f"/api/gifs/{b}", json={"description": "written"})
    stats = client.get("/api/library").json()["stats"]
    assert stats["total"] == 3
    assert stats["missing_tags"] == 2
    assert stats["missing_description"] == 2
    # Only the GIF with neither is missing both; the other two lack one each.
    assert stats["missing_either"] == 3
    assert stats["all"] == 3


def test_describe_by_scope_queues_only_that_scope(client, monkeypatch):
    from gifhole import enrich

    monkeypatch.setattr(enrich, "available", lambda: (True, ""))
    tagged = _add(client, "tagged.gif")
    _add(client, "bare.gif")
    client.patch(f"/api/gifs/{tagged}", json={"tags": "has"})
    res = client.post("/api/gifs/describe", json={"scope": "missing_tags"})
    assert res.json()["queued"] == 1


def test_describe_scope_all_covers_everything(client, monkeypatch):
    from gifhole import enrich

    monkeypatch.setattr(enrich, "available", lambda: (True, ""))
    for i in range(3):
        _add(client, f"s{i}.gif")
    assert client.post("/api/gifs/describe", json={"scope": "all"}).json()["queued"] == 3


def test_an_unknown_scope_is_refused(client, monkeypatch):
    from gifhole import enrich

    monkeypatch.setattr(enrich, "available", lambda: (True, ""))
    _add(client, "z.gif")
    assert client.post("/api/gifs/describe", json={"scope": "everything"}).status_code == 400


def test_cancel_endpoint_reports_what_it_stopped(client, monkeypatch):
    """A long describe run must be stoppable: 150 queued GIFs is 150 calls."""
    from gifhole import enrich

    monkeypatch.setattr(enrich, "available", lambda: (True, ""))
    # Fail every call, so jobs finish instantly without touching the network
    # and the queue still has to be drained by cancelling.
    monkeypatch.setattr(
        enrich, "describe_gif", lambda *a, **k: (_ for _ in ()).throw(enrich.EnrichError("no"))
    )
    for i in range(4):
        _add(client, f"c{i}.gif")
    client.post("/api/gifs/describe", json={"scope": "all"})
    res = client.post("/api/jobs/cancel", json={})
    assert res.status_code == 200
    assert res.json()["cancelled"] >= 0


def test_cancel_with_nothing_queued_is_harmless(client):
    assert client.post("/api/jobs/cancel", json={}).json() == {"ok": True, "cancelled": 0}


# -- optional shared token ---------------------------------------------------


@pytest.fixture
def guarded(tmp_path):
    """A library that requires a token, as it would be when exposed."""
    from gifhole.app import create_app

    return TestClient(create_app(tmp_path, auto_ocr=False, token="s3cret"))


def test_without_a_token_configured_nothing_changes(client):
    """The default is a loopback install, which must not grow a login."""
    assert client.get("/api/gifs").status_code == 200


def test_the_gif_files_are_guarded_too(guarded):
    """Protecting the API while serving the GIFs to anyone would be theatre:
    the files are the data."""
    assert guarded.get("/gifs/anything.gif").status_code == 401
    assert guarded.get("/").status_code == 401


def test_destructive_routes_are_refused_without_a_token(guarded):
    """The demonstration that prompted this: two unauthenticated calls used to
    clear a library and then permanently empty its trash."""
    assert guarded.post("/api/gifs/clear", json={"confirm": "clear"}).status_code == 401
    assert guarded.post("/api/trash/purge", json={"all": True}).status_code == 401


def test_a_wrong_token_is_refused(guarded):
    assert guarded.get("/api/gifs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert guarded.get("/api/gifs", params={"token": "wrong"}).status_code == 401


def test_a_bearer_header_is_accepted(guarded):
    res = guarded.get("/api/gifs", headers={"Authorization": "Bearer s3cret"})
    assert res.status_code == 200


def test_the_query_parameter_sets_a_cookie_so_images_work(guarded):
    """An <img> tag cannot carry an Authorization header, so without the cookie
    the UI would authenticate its fetches and still show broken images."""
    res = guarded.get("/", params={"token": "s3cret"})
    assert res.status_code == 200
    assert res.cookies.get("gifhole_token") == "s3cret"
    # The client keeps the cookie, so a bare request now succeeds.
    assert guarded.get("/api/gifs").status_code == 200


def test_the_token_can_come_from_the_environment(tmp_path, monkeypatch):
    from gifhole.app import create_app

    monkeypatch.setenv("GIFHOLE_TOKEN", "from-env")
    client = TestClient(create_app(tmp_path, auto_ocr=False))
    assert client.get("/api/gifs").status_code == 401
    assert client.get("/api/gifs", headers={"Authorization": "Bearer from-env"}).status_code == 200


# -- read-only token ---------------------------------------------------------


@pytest.fixture
def shared(tmp_path):
    """A library with a writer token and a look-but-not-touch token."""
    from gifhole.app import create_app

    return TestClient(create_app(tmp_path, auto_ocr=False, token="owner", read_token="guest"))


def _as(client, token):
    return {"Authorization": f"Bearer {token}"}


def test_a_reader_can_browse(shared):
    assert shared.get("/api/gifs", headers=_as(shared, "guest")).status_code == 200
    assert shared.get("/api/jobs", headers=_as(shared, "guest")).status_code == 200


def test_a_reader_cannot_change_anything(shared):
    """Blunt on purpose: anything that is not a read is refused, so a new route
    cannot quietly become writable by a guest."""
    guest = _as(shared, "guest")
    assert shared.post("/api/gifs/delete", json={"ids": [1]}, headers=guest).status_code == 403
    assert (
        shared.post("/api/gifs/clear", json={"confirm": "clear"}, headers=guest).status_code == 403
    )
    assert shared.patch("/api/gifs/1", json={"tags": "x"}, headers=guest).status_code == 403
    assert shared.post("/api/rescan", headers=guest).status_code == 403
    assert shared.delete("/api/gifs/1", headers=guest).status_code == 403


def test_the_owner_token_still_writes(shared):
    assert shared.post("/api/rescan", headers=_as(shared, "owner")).status_code == 200


def test_a_reader_is_told_which_it_is(shared):
    """So the UI can hide what it cannot do instead of offering 403s."""
    caps = shared.get("/api/jobs", headers=_as(shared, "guest")).json()["capabilities"]
    assert caps["read_only"] is True
    caps = shared.get("/api/jobs", headers=_as(shared, "owner")).json()["capabilities"]
    assert caps["read_only"] is False


def test_a_read_token_alone_does_nothing(tmp_path):
    """It would otherwise read as configured while leaving writes wide open."""
    from gifhole.app import create_app

    client = TestClient(create_app(tmp_path, auto_ocr=False, read_token="guest"))
    assert client.get("/api/gifs").status_code == 200
    assert client.get("/api/gifs", headers={"Authorization": "Bearer guest"}).status_code == 200

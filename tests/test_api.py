import pytest

from tests.conftest import make_gif


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


def _add(client, name="a.gif"):
    res = client.post("/api/gifs", files={"file": (name, make_gif(), "image/gif")})
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
        "/api/gifs", files={"file": ("a.gif", make_gif(), "image/gif")}, data={"tags": "cat"}
    ).json()["id"]
    b = client.post("/api/gifs", files={"file": ("b.gif", make_gif(), "image/gif")}).json()["id"]
    res = client.post("/api/gifs/tag", json={"ids": [a, b], "add": "reaction meme"})
    assert res.json()["changed"] == 2
    by_name = {g["filename"]: g["tags"] for g in client.get("/api/gifs").json()["gifs"]}
    assert by_name["a.gif"] == ["cat", "reaction", "meme"]
    assert by_name["b.gif"] == ["reaction", "meme"]


def test_bulk_tag_removes(client):
    ids = [
        client.post(
            "/api/gifs",
            files={"file": (f"r{i}.gif", make_gif(), "image/gif")},
            data={"tags": "todo keep"},
        ).json()["id"]
        for i in range(2)
    ]
    client.post("/api/gifs/tag", json={"ids": ids, "remove": "todo"})
    for gif in client.get("/api/gifs").json()["gifs"]:
        assert gif["tags"] == ["keep"]


def test_bulk_tag_can_add_and_remove_in_one_call(client):
    gif_id = client.post(
        "/api/gifs", files={"file": ("s.gif", make_gif(), "image/gif")}, data={"tags": "old"}
    ).json()["id"]
    client.post("/api/gifs/tag", json={"ids": [gif_id], "add": "new", "remove": "old"})
    assert client.get("/api/gifs").json()["gifs"][0]["tags"] == ["new"]


def test_bulk_tag_reports_only_what_actually_changed(client):
    """Re-applying a tag every GIF already has must not count as a write."""
    gif_id = client.post(
        "/api/gifs", files={"file": ("t.gif", make_gif(), "image/gif")}, data={"tags": "same"}
    ).json()["id"]
    res = client.post("/api/gifs/tag", json={"ids": [gif_id], "add": "same"})
    assert res.json() == {"ok": True, "changed": 0, "asked": 1}


def test_bulk_tag_needs_ids_and_tags(client):
    gif_id = client.post("/api/gifs", files={"file": ("u.gif", make_gif(), "image/gif")}).json()[
        "id"
    ]
    assert client.post("/api/gifs/tag", json={"ids": [], "add": "x"}).status_code == 400
    assert client.post("/api/gifs/tag", json={"ids": [gif_id], "add": "  "}).status_code == 400


def test_bulk_tag_skips_ids_that_are_gone(client):
    gif_id = client.post("/api/gifs", files={"file": ("v.gif", make_gif(), "image/gif")}).json()[
        "id"
    ]
    res = client.post("/api/gifs/tag", json={"ids": [gif_id, 9999], "add": "here"})
    assert res.json() == {"ok": True, "changed": 1, "asked": 2}


# -- batch describe ----------------------------------------------------------


def test_describe_batch_degrades_when_enrichment_is_unavailable(client, monkeypatch):
    """No key must mean a clear 503, never a half-run batch."""
    from gifhole import enrich

    monkeypatch.setattr(enrich, "available", lambda: (False, "no anthropic package"))
    gif_id = client.post("/api/gifs", files={"file": ("d.gif", make_gif(), "image/gif")}).json()[
        "id"
    ]
    res = client.post("/api/gifs/describe", json={"ids": [gif_id]})
    assert res.status_code == 503
    assert res.json()["detail"] == "no anthropic package"


def test_describe_batch_needs_a_selection(client, monkeypatch):
    from gifhole import enrich

    monkeypatch.setattr(enrich, "available", lambda: (True, ""))
    assert client.post("/api/gifs/describe", json={"ids": []}).status_code == 400

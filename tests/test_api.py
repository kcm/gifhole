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

import httpx
import pytest

from gifhole import fetch

# -- SSRF: the guard must run on every redirect hop ---------------------------


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_redirect_to_a_private_address_is_refused():
    """A public URL that 302s to loopback must not be followed.

    httpx's own follow_redirects skips the guard after the first URL, which
    turned /api/preview into a full-read SSRF against internal services.
    """

    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:80/secret.gif"})
        return httpx.Response(200, content=b"GIF89a-internal-secret")

    with _mock_client(handler) as client, pytest.raises(fetch.FetchError, match="private address"):
        fetch.download("http://example.com/lure.gif", client)


def test_redirect_to_a_public_address_still_works():
    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://cdn.example.net/real.gif"})
        return httpx.Response(200, content=b"GIF89a-ok")

    with _mock_client(handler) as client:
        assert fetch.download("http://example.com/lure.gif", client) == b"GIF89a-ok"


def test_redirect_chains_are_capped():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://example.com/next.gif"})

    with (
        _mock_client(handler) as client,
        pytest.raises(fetch.FetchError, match="too many redirects"),
    ):
        fetch.download("http://example.com/start.gif", client)


@pytest.mark.parametrize("addr", ["100.64.0.1", "224.0.0.1", "0.0.0.0", "169.254.169.254"])
def test_extra_reserved_ranges_are_refused(addr):
    """CGNAT, multicast, and unspecified are not covered by `is_private`."""
    with pytest.raises(fetch.FetchError):
        fetch.ensure_public_http_url(f"http://{addr}/x.gif")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a.gif",
        "http://localhost/a.gif",
        "http://127.0.0.1/a.gif",
        "http://192.168.1.1/a.gif",
        "http://[::1]/a.gif",
    ],
)
def test_private_and_non_http_urls_are_refused(url):
    """The server fetches on the user's behalf, so it must not probe the LAN."""
    with pytest.raises(fetch.FetchError):
        fetch.ensure_public_http_url(url)


def test_imgur_gifv_is_rewritten_to_mp4():
    assert (
        fetch.normalize_known_hosts("https://i.imgur.com/abc123.gifv")
        == "https://i.imgur.com/abc123.mp4"
    )


def test_unknown_hosts_pass_through_untouched():
    url = "https://example.com/a.gif"
    assert fetch.normalize_known_hosts(url) == url


def test_looks_like_url():
    assert fetch.looks_like_url("https://example.com/a.gif")
    assert not fetch.looks_like_url("just some text")
    assert not fetch.looks_like_url("example.com/a.gif")


PAGE = """
<html><head><title>Reaction</title>
  <meta property="og:image" content="https://cdn.example.com/hero.gif">
  <meta property="og:video" content="https://cdn.example.com/clip.mp4">
</head><body>
  <img src="/relative/one.gif" alt="One">
  <img data-src="https://cdn.example.com/lazy.gif" alt="Lazy">
  <img src="https://cdn.example.com/photo.jpg" alt="Not a gif">
  <video src="https://cdn.example.com/inline.webm"></video>
  <a href="https://cdn.example.com/linked.gif">download</a>
  <a href="https://example.com/about">about</a>
</body></html>
"""


def test_scrape_finds_gifs_and_videos_across_element_types():
    found = {c.url: c.kind for c in fetch.candidates_from_html(PAGE, "https://example.com/p")}
    assert found["https://cdn.example.com/hero.gif"] == "gif"
    assert found["https://cdn.example.com/lazy.gif"] == "gif"
    assert found["https://cdn.example.com/linked.gif"] == "gif"
    assert found["https://cdn.example.com/clip.mp4"] == "video"
    assert found["https://cdn.example.com/inline.webm"] == "video"


def test_scrape_resolves_relative_urls():
    found = {c.url for c in fetch.candidates_from_html(PAGE, "https://example.com/p")}
    assert "https://example.com/relative/one.gif" in found


def test_scrape_ignores_non_media():
    found = {c.url for c in fetch.candidates_from_html(PAGE, "https://example.com/p")}
    assert "https://cdn.example.com/photo.jpg" not in found
    assert "https://example.com/about" not in found


def test_scrape_of_page_without_media_is_empty():
    assert fetch.candidates_from_html("<html><body><p>hi</p></body></html>", "https://x.com") == []


def test_dedupe_prefers_full_size_over_thumbnail():
    """A page offering one image at several sizes must yield one GIF, not three."""
    kept = fetch.dedupe_sizes(
        [
            fetch.Candidate("https://x.com/thumb/a/ab/Cat.gif/250px-Cat.gif", "gif"),
            fetch.Candidate("https://x.com/a/ab/Cat.gif", "gif"),
            fetch.Candidate("https://x.com/thumb/a/ab/Cat.gif/500px-Cat.gif", "gif"),
        ]
    )
    assert [c.url for c in kept] == ["https://x.com/a/ab/Cat.gif"]


def test_dedupe_keeps_genuinely_different_images():
    kept = fetch.dedupe_sizes(
        [
            fetch.Candidate("https://x.com/Cat.gif", "gif"),
            fetch.Candidate("https://x.com/Dog.gif", "gif"),
        ]
    )
    assert len(kept) == 2


@pytest.mark.parametrize(
    ("url", "title", "expected"),
    [
        # A descriptive URL filename wins over the page title.
        ("https://x.com/a/Rotating_earth.gif", "GIF - Wikipedia", "Rotating_earth.gif"),
        # Giphy names every file giphy.gif, so the title is all we have.
        ("https://media.giphy.com/media/xyz/giphy.gif", "Excited Cat GIF", "Excited Cat GIF.gif"),
        # Thumbnail prefixes are stripped from the name.
        ("https://x.com/thumb/250px-Cat.gif", "", "Cat.gif"),
        ("https://x.com/media/", "", "download.gif"),
    ],
)
def test_filename_selection(url, title, expected):
    assert fetch._filename_for(url, title) == expected


# -- reddit ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.reddit.com/r/gifs/comments/a/b/", True),
        ("https://old.reddit.com/r/gifs/", True),
        ("https://reddit.com/r/gifs/", True),
        ("https://notreddit.com/r/gifs/", False),
        ("https://example.com/reddit.com/x", False),
    ],
)
def test_is_reddit(url, expected):
    assert fetch.is_reddit(url) is expected


def test_reddit_rewrites_to_old_host():
    """www serves a JS shell and .json 403s bots; old.reddit renders HTML."""
    assert (
        fetch.to_old_reddit("https://www.reddit.com/r/gifs/comments/a/b/")
        == "https://old.reddit.com/r/gifs/comments/a/b/"
    )


# Mirrors old.reddit.com: the post carries data-url, comments come after.
OLD_REDDIT = """
<html><head><meta property="og:title" content="Greatest GIF of all time"></head>
<body>
  <div class="thing link" data-url="https://i.redd.it/postgif.gif">
    <a class="title" href="https://i.redd.it/postgif.gif">Greatest GIF of all time</a>
  </div>
  <div class="commentarea">
    <div class="thing comment"><a href="https://i.redd.it/commentgif.gif">nice</a></div>
    <div class="thing comment"><a href="https://i.redd.it/another.gif">also nice</a></div>
  </div>
</body></html>
"""


def test_reddit_collects_every_gif_on_the_page():
    """A page URL means the whole page: post GIF *and* every comment GIF."""
    found = {c.url for c in fetch.candidates_from_html(OLD_REDDIT, "https://old.reddit.com/r/g/")}
    assert found == {
        "https://i.redd.it/postgif.gif",
        "https://i.redd.it/commentgif.gif",
        "https://i.redd.it/another.gif",
    }


# -- staging: preview and import share one download ---------------------------


def test_stage_path_is_stable_and_per_url(tmp_path):
    a = fetch.stage_path(tmp_path, "https://x.com/a.gif")
    assert a == fetch.stage_path(tmp_path, "https://x.com/a.gif")
    assert a != fetch.stage_path(tmp_path, "https://x.com/b.gif")


def test_staged_bytes_are_reused_without_refetching(tmp_path):
    """The selection screen proxies previews; importing must not re-download."""
    from tests.conftest import make_gif

    url = "https://example.com/already-staged.gif"
    staged = fetch.stage_path(tmp_path, url)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(make_gif())

    # No client passed and no network available to this test: a cache miss
    # would attempt a real request, so returning bytes proves the cache hit.
    assert fetch.fetch_bytes_cached(url, tmp_path) == make_gif()


def test_import_urls_uses_staged_bytes(store, tmp_path):
    from tests.conftest import make_gif

    url = "https://example.com/staged-import.gif"
    staged = fetch.stage_path(tmp_path, url)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(make_gif())

    report = fetch.import_urls(store, [url], tmp_path, {url: "Staged Import"})
    assert report.added == ["staged-import.gif"]
    assert report.skipped == []
    assert store.list_gifs()[0].source_url == url


def test_import_urls_reports_failures_without_aborting(store, tmp_path):
    """One bad URL must not sink the rest of the selection."""
    from tests.conftest import make_gif

    good = "https://example.com/good.gif"
    fetch.stage_path(tmp_path, good).parent.mkdir(parents=True, exist_ok=True)
    fetch.stage_path(tmp_path, good).write_bytes(make_gif())
    bad = "https://example.com/not-a.gif"
    fetch.stage_path(tmp_path, bad).write_bytes(b"<html>nope</html>")

    report = fetch.import_urls(store, [good, bad], tmp_path)
    assert report.added == ["good.gif"]
    assert [u for u, _ in report.skipped] == [bad]


def test_import_reports_progress_after_each_url(store, tmp_path):
    """The rail's live count depends on this: a batch that reported only at the
    end left one row sitting at zero for the whole import, which reads as a
    stall. One call per URL, added or skipped, with a running index."""
    from tests.conftest import make_gif

    good = "https://example.com/good.gif"
    fetch.stage_path(tmp_path, good).parent.mkdir(parents=True, exist_ok=True)
    fetch.stage_path(tmp_path, good).write_bytes(make_gif())
    bad = "https://example.com/not-a.gif"
    fetch.stage_path(tmp_path, bad).write_bytes(b"<html>nope</html>")

    seen = []
    fetch.import_urls(
        store, [good, bad], tmp_path, on_progress=lambda i, n, label: seen.append((i, n, label))
    )

    # Called for both, in order, counting up to the total.
    assert [(i, n) for i, n, _ in seen] == [(1, 2), (2, 2)]
    assert seen[0][2] == "good.gif", "the added file names its progress row"
    assert seen[1][2] == "", "a skipped URL reports no filename"


def test_a_failing_progress_callback_never_loses_the_import(store, tmp_path):
    """The rail is a nicety; the import is not. A callback that throws must not
    take the download with it."""
    from tests.conftest import make_gif

    url = "https://example.com/good.gif"
    fetch.stage_path(tmp_path, url).parent.mkdir(parents=True, exist_ok=True)
    fetch.stage_path(tmp_path, url).write_bytes(make_gif())

    def boom(*_):
        raise RuntimeError("rail blew up")

    report = fetch.import_urls(store, [url], tmp_path, on_progress=boom)
    assert report.added == ["good.gif"]


def test_data_url_attribute_is_scraped():
    """old.reddit hangs post media off data-url rather than an <a> or <img>."""
    html = '<html><body><div data-url="https://i.redd.it/viadata.gif"></div></body></html>'
    found = {c.url for c in fetch.candidates_from_html(html, "https://old.reddit.com/")}
    assert "https://i.redd.it/viadata.gif" in found

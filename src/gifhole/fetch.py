"""Pulling GIFs off the web, from a direct link or by scraping a page.

The awkward part is that most "GIFs" on the web are not GIFs. Giphy, Tenor,
Reddit, and Imgur all serve MP4/WebM and only hand out a real .gif on request,
so matching `img[src$=.gif]` finds almost nothing on the sites you actually
want. This module treats video as a first-class source and converts it with
ffmpeg, falling back gracefully when ffmpeg is absent.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# A browser-like User-Agent. Several media hosts (Reddit especially) serve a
# JS-only shell or a 403 to obvious bots, so an honest "gifhole/0.1" UA silently
# comes back empty. We only ever fetch media the user explicitly pasted.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MAX_BYTES = 64 * 1024 * 1024
TIMEOUT = 20.0
MAX_REDIRECTS = 5

GIF_EXT = (".gif",)
VIDEO_EXT = (".mp4", ".webm", ".m4v", ".mov")

# Converted clips are capped: a 3-minute video makes a useless 200MB GIF.
MAX_CLIP_SECONDS = 15
CONVERT_FPS = 15
CONVERT_WIDTH = 480


class FetchError(Exception):
    """A download or conversion failed in a way worth showing the user."""


@dataclass
class Candidate:
    url: str
    kind: str  # "gif" | "video"
    title: str = ""


@dataclass
class FetchReport:
    added: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (url, reason)

    def as_dict(self) -> dict:
        return {
            "added": self.added,
            "skipped": [{"url": u, "reason": r} for u, r in self.skipped],
        }


# -- safety ------------------------------------------------------------------


# Carrier-grade NAT, which `ipaddress` does not report as private.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (ip.version == 4 and ip in _CGNAT)
    )


def ensure_public_http_url(url: str) -> None:
    """Reject anything that is not a public http(s) endpoint.

    The server fetches URLs on the user's behalf, so it must not be talked into
    reading `file://` or probing hosts on the local network.

    This runs on every redirect hop, not just the URL the user typed. See
    `_guarded_get` and `download`.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"unsupported scheme: {parsed.scheme or 'none'}")
    if not parsed.hostname:
        raise FetchError("no hostname in URL")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise FetchError(f"cannot resolve {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_forbidden(ip):
            raise FetchError(f"refusing to fetch a private address ({ip})")


# -- discovery ---------------------------------------------------------------


def _classify(url: str) -> str | None:
    path = urlparse(url).path.lower()
    if path.endswith(GIF_EXT):
        return "gif"
    if path.endswith(VIDEO_EXT):
        return "video"
    return None


def normalize_known_hosts(url: str) -> str:
    """Rewrite platform URLs that have a well-known direct-media form."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.")
    # Imgur's .gifv is an HTML shell around an MP4 of the same name.
    if host.endswith("imgur.com") and parsed.path.endswith(".gifv"):
        return url[: -len(".gifv")] + ".mp4"
    return url


def candidates_from_html(html: str, base_url: str) -> list[Candidate]:
    """Pull every plausible GIF or video URL out of a page.

    Open Graph tags come first deliberately: on Giphy and Tenor they point
    straight at the real media, which saves guessing at their URL schemes.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Candidate] = {}

    def offer(raw: str | None, title: str = "") -> None:
        if not raw:
            return
        absolute = urljoin(base_url, raw.strip())
        kind = _classify(absolute)
        if kind and absolute not in found:
            found[absolute] = Candidate(absolute, kind, title)

    page_title = soup.title.string.strip() if soup.title and soup.title.string else ""

    for prop in ("og:image", "og:video", "og:video:secure_url", "twitter:image"):
        for tag in soup.find_all("meta", attrs={"property": prop}):
            offer(tag.get("content"), page_title)
        for tag in soup.find_all("meta", attrs={"name": prop}):
            offer(tag.get("content"), page_title)

    for img in soup.find_all("img"):
        offer(img.get("src"), img.get("alt", ""))
        offer(img.get("data-src"), img.get("alt", ""))
        srcset = img.get("srcset") or ""
        for part in srcset.split(","):
            offer(part.strip().split(" ")[0] if part.strip() else None, img.get("alt", ""))

    for video in soup.find_all("video"):
        offer(video.get("src"), page_title)
    for source in soup.find_all("source"):
        offer(source.get("src"), page_title)
    for anchor in soup.find_all("a"):
        offer(anchor.get("href"), anchor.get_text(strip=True))
    # old.reddit hangs the media off a data-url attribute rather than an <a>.
    for tag in soup.find_all(attrs={"data-url": True}):
        offer(tag.get("data-url"), page_title)

    return list(found.values())


# -- reddit ------------------------------------------------------------------


def is_reddit(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "reddit.com" or host.endswith(".reddit.com")


def to_old_reddit(url: str) -> str:
    """old.reddit.com serves server-rendered HTML with the media inline;
    www.reddit.com serves a JS shell, and the `.json` API now 403s bots."""
    return urlparse(url)._replace(netloc="old.reddit.com").geturl()


# Reddit needs no special extraction beyond the host rewrite: once we have
# old.reddit's server-rendered HTML the generic scraper collects everything on
# the page, post and comments alike. A page URL means the whole page.


# -- transfer ----------------------------------------------------------------


def _client() -> httpx.Client:
    # Redirects are followed by hand (see _guarded_get / download) so the SSRF
    # guard runs on every hop. httpx's own redirect handling validates nothing
    # after the first URL, which lets a public host 302 to loopback and read an
    # internal service through /api/preview.
    return httpx.Client(
        follow_redirects=False,
        timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )


def _next_hop(response: httpx.Response) -> str:
    location = response.headers.get("location")
    if not location:
        raise FetchError("redirect with no Location header")
    return str(response.url.join(location))


def _guarded_get(client: httpx.Client, url: str) -> httpx.Response:
    """GET, re-checking the SSRF guard on every redirect hop."""
    for _ in range(MAX_REDIRECTS + 1):
        ensure_public_http_url(url)
        response = client.get(url)
        if response.is_redirect:
            url = _next_hop(response)
            continue
        return response
    raise FetchError(f"too many redirects (over {MAX_REDIRECTS})")


def download(url: str, client: httpx.Client | None = None) -> bytes:
    """Fetch a URL into memory, refusing anything oversized.

    Redirects are followed manually so the SSRF guard is re-applied to each
    hop; see `_client`.
    """
    owned = client is None
    client = client or _client()
    try:
        for _ in range(MAX_REDIRECTS + 1):
            ensure_public_http_url(url)
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = _next_hop(response)
                    continue
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and int(declared) > MAX_BYTES:
                    raise FetchError(f"too large ({int(declared) // 1024 // 1024}MB)")
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise FetchError("too large (exceeded limit mid-download)")
                    chunks.append(chunk)
                return b"".join(chunks)
        raise FetchError(f"too many redirects (over {MAX_REDIRECTS})")
    except httpx.HTTPError as exc:
        raise FetchError(f"download failed: {exc}") from exc
    finally:
        if owned:
            client.close()


# -- conversion --------------------------------------------------------------


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def video_to_gif(data: bytes, suffix: str = ".mp4") -> bytes:
    """Convert a video clip to a GIF via ffmpeg's two-pass palette filter."""
    if not ffmpeg_available():
        raise FetchError("ffmpeg is not installed, so video cannot be converted")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"in{suffix}"
        dest = Path(tmp) / "out.gif"
        src.write_bytes(data)
        # Generating a palette first avoids the muddy 256-colour default.
        # min(w,iw) caps without upscaling. Enlarging a small clip multiplies
        # the file size for no gain in quality.
        vf = (
            f"fps={CONVERT_FPS},scale='min({CONVERT_WIDTH},iw)':-2:flags=lanczos,"
            "split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer"
        )
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-t",
                str(MAX_CLIP_SECONDS),
                "-i",
                str(src),
                "-vf",
                vf,
                "-loop",
                "0",
                str(dest),
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0 or not dest.exists():
            tail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
            raise FetchError("ffmpeg failed: " + " / ".join(tail))
        return dest.read_bytes()


# -- orchestration -----------------------------------------------------------


# Media hosts that name every file the same thing, so the URL tells you nothing.
GENERIC_STEMS = {"giphy", "tenor", "index", "image", "video", "media", "download", "raw"}

# MediaWiki and many CMSes prefix a size onto the thumbnail's filename.
THUMB_PREFIX = re.compile(r"^\d{2,4}px-")


def _filename_for(url: str, title: str) -> str:
    """Prefer the URL's own filename; fall back to the page title.

    Giphy and Tenor call every file `giphy.gif` / `tenor.gif`, where the page
    title ("Excited Cat GIF") is the only descriptive name available.
    """
    stem = THUMB_PREFIX.sub("", Path(urlparse(url).path).stem)
    if len(stem) >= 3 and stem.lower() not in GENERIC_STEMS:
        return f"{stem}.gif"
    if title:
        return f"{title[:60]}.gif"
    return "download.gif"


def dedupe_sizes(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse thumbnail/full-size pairs of the same image down to the best one.

    A page that shows GIFs via `srcset` or MediaWiki thumbs offers the same
    image at several sizes; without this you get every size in the library.
    """
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        path = urlparse(candidate.url).path
        key = THUMB_PREFIX.sub("", Path(path).name).lower()
        incumbent = best.get(key)
        if incumbent is None or _prefer(candidate, incumbent):
            best[key] = candidate
    return list(best.values())


def _prefer(candidate: Candidate, incumbent: Candidate) -> bool:
    """True when `candidate` looks like the fuller-size version."""

    def thumbiness(c: Candidate) -> tuple[int, int]:
        path = urlparse(c.url).path
        return (
            "/thumb/" in path,
            bool(THUMB_PREFIX.match(Path(path).name)),
        )

    return thumbiness(candidate) < thumbiness(incumbent)


def stage_path(staging_dir: Path, url: str) -> Path:
    return staging_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".bin")


def fetch_bytes_cached(url: str, staging_dir: Path, client: httpx.Client | None = None) -> bytes:
    """Download once and reuse.

    Reddit's preview CDNs refuse cross-origin browser loads, so the selection
    screen previews through the server. Staging those bytes means confirming an
    import doesn't fetch everything a second time.
    """
    cached = stage_path(staging_dir, url)
    if cached.exists():
        return cached.read_bytes()
    data = download(url, client)
    staging_dir.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return data


def discover(url: str) -> tuple[str, list[Candidate]]:
    """Find what a URL offers without downloading any of it.

    Returns ("direct", [one]) for a media link, or ("page", [...]) for
    everything found on a page.
    """
    url = normalize_known_hosts(url.strip())
    ensure_public_http_url(url)

    direct = _classify(url)
    if direct:
        return "direct", [Candidate(url, direct)]

    with _client() as client:
        # Reddit only needs the host swapped; the scrape itself is generic.
        page_url = to_old_reddit(url) if is_reddit(url) else url
        try:
            page = _guarded_get(client, page_url)
            page.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"could not load page: {exc}") from exc
        if "html" not in page.headers.get("content-type", ""):
            raise FetchError("that URL is neither a GIF nor an HTML page")
        found = candidates_from_html(page.text, str(page.url))
        found = [Candidate(normalize_known_hosts(c.url), c.kind, c.title) for c in found]
        found = dedupe_sizes(found)

    if not found:
        raise FetchError("no GIFs or videos found on that page")
    return "page", found


def import_urls(
    store,
    urls: list[str],
    staging_dir: Path,
    titles: dict[str, str] | None = None,
    on_progress=None,
) -> FetchReport:
    """Download and add exactly the URLs given, reusing anything already staged.

    `on_progress(finished, total, label)` is called after each URL, whether it
    was added or skipped. Importing a thread is the longest thing gifhole does
    and it used to report nothing until the whole batch landed, so the queue
    showed one row sitting at zero for minutes with no way to tell a slow
    download from a wedged one.
    """
    report = FetchReport()
    titles = titles or {}
    with _client() as client:
        for index, url in enumerate(urls, 1):
            label = ""
            try:
                ensure_public_http_url(url)
                data = fetch_bytes_cached(url, staging_dir, client)
                if _classify(url) == "video":
                    suffix = Path(urlparse(url).path).suffix or ".mp4"
                    data = video_to_gif(data, suffix)
                gif = store.add_bytes(_filename_for(url, titles.get(url, "")), data, source_url=url)
                report.added.append(gif.filename)
                label = gif.filename
            except (FetchError, ValueError) as exc:
                report.skipped.append((url, str(exc)))
            if on_progress is not None:
                # Never let a reporting failure lose an import that worked.
                try:
                    on_progress(index, len(urls), label)
                except Exception:  # noqa: BLE001
                    log.debug("progress callback failed", exc_info=True)
    return report


def fetch_into(store, url: str, limit: int = 300, staging_dir: Path | None = None) -> FetchReport:
    """Resolve `url` to GIFs and add each one to the library.

    A direct media link is taken as-is; anything else is treated as a page to
    scrape. Videos are converted when ffmpeg is available and reported as
    skipped when it is not, rather than failing the whole batch.
    """
    report = FetchReport()
    _, targets = discover(url)

    # Never cap silently: a page URL means everything on the page, so if a
    # thread exceeds the ceiling, say so rather than quietly truncating.
    if len(targets) > limit:
        report.skipped.append(
            (f"{len(targets) - limit} more on the page", f"over the {limit} per-page limit")
        )
        targets = targets[:limit]

    staging = staging_dir or Path(tempfile.mkdtemp(prefix="gifhole-stage-"))
    picked = import_urls(
        store,
        [c.url for c in targets],
        staging,
        {c.url: c.title for c in targets},
    )
    report.added.extend(picked.added)
    report.skipped.extend(picked.skipped)
    return report


def looks_like_url(text: str) -> bool:
    return bool(re.match(r"^https?://\S+$", text.strip()))

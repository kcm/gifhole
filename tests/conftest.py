import ipaddress
import socket

import pytest
from fastapi.testclient import TestClient

from gifhole.app import create_app
from gifhole.store import Store


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """Keep the suite hermetic without weakening the SSRF guard.

    `ensure_public_http_url` resolves every URL it checks, so tests touching
    only the staging cache still hit real DNS. Stub the resolver, but keep it
    honest: an IP literal resolves to itself, so the guard's rejection tests
    still exercise real logic. Only names get a canned public answer.
    """

    def fake_getaddrinfo(host, port, *args, **kwargs):
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            # Mirror what a real resolver does for the names that matter.
            if host == "localhost" or str(host).endswith(".localhost"):
                ip = ipaddress.ip_address("127.0.0.1")
            else:
                ip = ipaddress.ip_address("93.184.216.34")  # any other name: public
        if ip.version == 6:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (str(ip), port or 80, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (str(ip), port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def make_gif(width: int = 8, height: int = 6) -> bytes:
    """A real single-frame GIF89a.

    Built with Pillow rather than hand-assembled: a byte-minimal GIF passes the
    magic-byte and header checks but is not actually decodable, which silently
    breaks anything that opens the frames.
    """
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("P", (width, height), color=1).save(buf, format="GIF")
    return buf.getvalue()


def make_animated_gif(width: int = 32, height: int = 24, frames: int = 6) -> bytes:
    """A real multi-frame GIF with visibly different frames.

    The frames must actually differ. Pillow collapses identical ones into a
    single frame, which quietly turns an "animated" fixture into a still.
    """
    import io

    from PIL import Image, ImageDraw

    images = []
    for i in range(frames):
        frame = Image.new("RGB", (width, height), (20, 20, 30))
        box = width / frames
        ImageDraw.Draw(frame).rectangle([i * box, 0, i * box + box, height], fill=(230, 140, 40))
        images.append(frame.convert("P", palette=Image.ADAPTIVE))

    buf = io.BytesIO()
    images[0].save(buf, format="GIF", save_all=True, append_images=images[1:], duration=80)
    return buf.getvalue()


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path)


@pytest.fixture
def client(tmp_path) -> TestClient:
    # OCR off: the suite must not depend on any engine being present, and
    # must not shell out to tesseract on machines that have it.
    return TestClient(create_app(tmp_path, auto_ocr=False))


def make_textured_gif(seed: int = 0, width: int = 160, height: int = 120) -> bytes:
    """A GIF with real structure, for perceptual-hash tests.

    Two properties matter and both bit during development. A flat image has no
    adjacent-pixel differences, so its dhash is zero and matches every other
    flat image. And a fine, high-frequency pattern aliases under downsampling,
    so a resized copy stops matching, which real GIF frames do not do. Hence
    large smooth shapes, positioned as fractions of the canvas.
    """
    import io
    import random

    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        draw.line([(0, y), (width, y)], fill=(30 + y * 120 // height, 60, 200 - y * 90 // height))
    for _ in range(3):
        x0, y0 = rnd.uniform(0, 0.5), rnd.uniform(0, 0.5)
        x1, y1 = x0 + rnd.uniform(0.25, 0.45), y0 + rnd.uniform(0.25, 0.45)
        shape = draw.ellipse if rnd.random() < 0.5 else draw.rectangle
        shape(
            [x0 * width, y0 * height, x1 * width, y1 * height],
            fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)),
        )
    buf = io.BytesIO()
    img.save(buf, format="GIF")
    return buf.getvalue()

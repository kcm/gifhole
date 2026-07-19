# gifhole in a container.
#
# Two jobs: somewhere to run the suite on Linux, and a way to run gifhole
# without a Python toolchain. The macOS-only features (Vision OCR, and the
# pasteboard trick that keeps a paste animated) are simply absent here and
# degrade to nothing, which is the behaviour this image is useful for proving.
FROM python:3.13-slim

# ffmpeg earns its place: Giphy, Tenor and Reddit all serve MP4 rather than
# GIF, so without it most URL imports would be skipped.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies before source, so editing code does not re-resolve the world.
COPY pyproject.toml uv.lock README.md ./
COPY src/gifhole/__init__.py src/gifhole/__init__.py
RUN uv sync --frozen --no-dev

COPY . .
RUN uv sync --frozen --no-dev

# The library lives on a volume, so it survives the container being replaced.
ENV GIFHOLE_ROOT=/library
VOLUME ["/library"]
EXPOSE 8777

# 0.0.0.0 inside the container, which is not the loosening it looks like: the
# container's network namespace is the boundary, and what is actually exposed
# is decided by the port mapping. Publish it as 127.0.0.1:8777:8777 to keep it
# on the host's loopback, which is what the compose file does.
CMD ["uv", "run", "gifhole", "--host", "0.0.0.0", "--port", "8777", "--no-open"]

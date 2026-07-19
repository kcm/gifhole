# Index

| Path | Purpose |
| --- | --- |
| [README.md](README.md) | Quickstart and usage |
| [AGENTS.md](AGENTS.md) | Agent guide: invariants, conventions, gotchas |
| [CHANGELOG.md](CHANGELOG.md) | Keep a Changelog history |
| [pyproject.toml](pyproject.toml) | Deps, scripts, ruff and pytest config |
| [src/gifhole/cli.py](src/gifhole/cli.py) | `gifhole` entry point; starts uvicorn |
| [src/gifhole/app.py](src/gifhole/app.py) | FastAPI routes (UI, files, JSON API, jobs) |
| [src/gifhole/store.py](src/gifhole/store.py) | SQLite metadata + filesystem operations |
| [src/gifhole/jobs.py](src/gifhole/jobs.py) | Background job queue (one worker thread) |
| [src/gifhole/frames.py](src/gifhole/frames.py) | Sampling still frames out of a GIF |
| [src/gifhole/ocr.py](src/gifhole/ocr.py) | Burned-in text via macOS Vision |
| [src/gifhole/enrich.py](src/gifhole/enrich.py) | Claude descriptions + meme ID (opt-in) |
| [src/gifhole/fetch.py](src/gifhole/fetch.py) | Download from a link or scrape a page |
| [src/gifhole/static/index.html](src/gifhole/static/index.html) | UI markup |
| [src/gifhole/static/style.css](src/gifhole/static/style.css) | Styles |
| [src/gifhole/static/app.js](src/gifhole/static/app.js) | Grid, search, copy, upload |
| [tests/conftest.py](tests/conftest.py) | Fixtures; builds real GIFs via Pillow |
| [tests/test_store.py](tests/test_store.py) | Store: naming, search, trash, rescan |
| [tests/test_api.py](tests/test_api.py) | HTTP API end to end |
| [tests/test_metadata.py](tests/test_metadata.py) | Frames, jobs, OCR-fed search, migration |
| [tests/test_fetch.py](tests/test_fetch.py) | URL safety, scraping, dedup, naming |
| [.claude/launch.json](.claude/launch.json) | Dev server config for the preview pane |
| [packaging/homebrew/gifhole.rb](packaging/homebrew/gifhole.rb) | Homebrew formula with `brew services` support |

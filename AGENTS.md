# gifhole agent guide

A local GIF library: a small FastAPI server over a folder of `.gif` files, with
a click-to-copy web UI. No build step for the frontend. Two optional layers add
metadata and remote download; both degrade to nothing when their dependency is
absent, so the core stays offline and no-key.

## Layout

See [INDEX.md](INDEX.md) for the full path -> purpose table.

- `src/gifhole/store.py`: SQLite metadata + filesystem operations
- `src/gifhole/app.py`: HTTP routes, and the OCR-on-add wiring
- `src/gifhole/jobs.py`: one-worker-thread job queue (OCR, scraping, enrich)
- `src/gifhole/frames.py`: sampling still frames out of a GIF
- `src/gifhole/ocr.py`: burned-in text via macOS Vision (local, free, offline)
- `src/gifhole/enrich.py`: Claude descriptions + meme ID (opt-in, needs a key)
- `src/gifhole/fetch.py`: download from a link or scrape a page; ffmpeg for video
- `src/gifhole/static/`: the UI (plain HTML/CSS/JS, no bundler)
- `tests/`: hermetic; no network, no Vision, no Claude, no ffmpeg

## Core invariant

**The `gifs/` folder is the source of truth.** SQLite only annotates it with
titles, tags, and copy counts. Deleting `gifhole.db` loses annotations but never
GIFs; `rescan()` reconciles the two directions. Keep it that way; never make
the database authoritative over what exists.

Deletes move files to `.trash/`, they do not unlink. Preserve that.

**`store.purge()` / `empty_trash()` are the only code in the project that
destroy data.** Everything else is recoverable, and that asymmetry is the whole
safety model: single deletes skip the confirm precisely because `z` restores
them. Route every trash filename through `_trash_path()`, which refuses
anything resolving outside `.trash/` (the names come from the client, so
`../gifs/keepme.gif` has to bounce there, not at the caller). `/api/trash/purge`
additionally refuses a bare call: name the entries or pass `all=true`, so a
stray request cannot empty it.

## Conventions

- `uv run pytest` and `uv run ruff check .` must both be clean before finishing.
- Frontend has no dependencies and no build. Keep it that way; plain DOM APIs.
- **The UI has selectable dot-com-era skins**, chosen from a muted picker in the
  footer (deliberately not a primary action) and persisted to `localStorage`
  under `gifhole-theme`. Each is an homage:
  `memepool` (default: cream/serif/blue), `fark` (purple/green/Arial),
  `zombo` (black void, neon, animated rings), `webvan` (grocery green),
  `petsdotcom` (blue+red, rounded), `altavista` (white portal, blue top band),
  `linkedin` (corporate, tongue-in-cheek). The retro aesthetic is
  deliberate, so don't "modernize" it away.
- **How theming works, and the contract to keep:** everything themeable is a CSS
  custom property on `:root`; `:root[data-theme="x"]` blocks override them. The
  markup and every JS class hook are theme-agnostic. A skin is variables plus a
  few structural flourishes (zombo's `body::before` rings + `@keyframes`,
  altavista's `body` top border, linkedin's radius/shadow). Add a new skin by
  adding a `[data-theme]` block + a `<select>` option + a `TAGLINES` entry;
  don't special-case theme names in the rendering JS.
- **A skin may rename the masthead** via the `WORDMARKS` map in app.js (default
  is the `PRODUCT` constant). Only `linkedin` overrides it, to `linkedin`, so the
  skin brands the whole site as LinkedIn would. Keep this data-driven:
  add a `WORDMARKS` entry, don't branch on the theme name.
- **No flash of the wrong skin:** an inline `<head>` script sets
  `documentElement.dataset.theme` from `localStorage` before the stylesheet
  paints. `applyTheme()` in app.js (runs on load and on change) syncs the footer
  `<select>`, swaps the per-skin `.tagline` and masthead wordmark, and re-saves.
  Keep the head script tiny and dependency-free.
- Never interpolate a filename, title, or tag into `innerHTML`. Static template
  markup only, values set via `textContent` / properties.
- Uploads are validated by magic bytes (`GIF87a`/`GIF89a`), not by extension or
  content-type. Filenames are slugged through `safe_filename()`.
- **The metadata/download layers must never become required.** OCR checks
  `vision_available()`, enrich checks `enrich.available()`, video checks
  `ffmpeg_available()`. Each returns cleanly when its dependency is missing,
  and a failed job is recorded, not raised. Don't add a hard import of
  `anthropic`, `Vision`, or `ffmpeg` at module top level in the request path.
- **New DB columns go in `MIGRATIONS`, not just `SCHEMA`.** `SCHEMA` only runs
  for a fresh database; existing ones are patched by `_migrate()`. Adding a
  column to `SCHEMA` alone silently breaks every existing library.
- **A page URL means the whole page.** Never narrow a scrape to the "main"
  item, and never cap silently: if a bound is hit, report what was left out.
- **The SSRF guard must run on every redirect hop.** `_client()` sets
  `follow_redirects=False` on purpose: httpx's own redirect handling validates
  nothing after the first URL, which made a public page that 302s to loopback a
  full-read SSRF through `/api/preview`. Use `_guarded_get()` or `download()`,
  which re-check each hop and cap the chain; never call `client.get` directly on
  a user-supplied URL.
- **Develop with `uv run gifhole --reload`.** Editing source under a plainly
  started server means testing yesterday's code, which has burned real debugging
  time here. The flag can't hand uvicorn the app instance the normal path builds
  (the reloader rebuilds it in a subprocess, so it needs the import string plus
  `factory=True`), so `--root` travels via the `GIFHOLE_ROOT` environment
  variable instead. Keep both halves if you touch `cli.py`.
- **No module-level `app = create_app()`.** Building one at import time created a
  real library under `~/.gifhole`, started a worker thread, and ran Vision OCR
  over the user's actual GIFs whenever anything imported the module, the test
  suite included. Serve with `uvicorn --factory gifhole.app:create_app`.
- **Mutating routes refuse cross-site requests.** There is no auth and several
  routes are CORS-simple (a bodyless POST to `/enrich` spends real API credit),
  so a middleware rejects writes carrying a cross-site `Sec-Fetch-Site` or a
  foreign `Origin`. Reads are unaffected, and non-browser clients send neither.
- The server fetches arbitrary URLs, so `fetch.ensure_public_http_url()` gates
  every download against `file://`, non-http schemes, and private/loopback IPs.
  Call it before any new fetch path, not just at the entry point.
- **Reddit needs its own path, and so may other platforms.** `www.reddit.com`
  serves a JS-only shell (no media in the HTML) and the `.json` API 403s
  non-browser clients, so the generic scraper finds nothing. `fetch.py` rewrites
  Reddit URLs to `old.reddit.com`; the scrape itself stays generic, so a page URL
  yields every GIF on the page, comments included. `USER_AGENT` is browser-like
  for the same reason. Before claiming a platform is supported, fetch a real URL
  from it; a generic scraper working elsewhere proves nothing.

## Gotchas

- **`[hidden]` needs the `!important` reset in `style.css`.** Author `display`
  rules outrank the UA style for the `hidden` attribute, so a `.drop`/`.toast`
  element will keep painting while `el.hidden === true`. Reading `.hidden` in
  the console will lie to you; check a screenshot.
- Clipboard writes need a real user gesture. Synthesized `MouseEvent`s fail with
  `NotAllowedError`. That is the test harness, not a bug.
- **Browsers will not accept `image/gif` on the clipboard**
  (`ClipboardItem.supports("image/gif") === false`), so no amount of frontend
  work will paste an animation. Do not "fix" this by writing a GIF blob; it
  silently no-ops. The way out is not the browser: on macOS,
  `clipboard.copy_file()` writes the real file to the pasteboard via AppKit, and
  `POST /api/gifs/{id}/clipboard` exposes it, which is what makes Discord upload
  an animated GIF instead of a still. Two consequences to preserve: that path
  needs the server (unlike the PNG fallback), and it is gated on the
  `file_clipboard` capability so non-Mac clients degrade to the PNG rather than
  erroring. Verify a change here by reading the pasteboard back, not by trusting
  the toast.
- **A byte-minimal hand-built GIF passes the magic-byte check but is not
  decodable.** Pillow rejects it as "image file is truncated". Test fixtures use
  `make_gif()` / `make_animated_gif()` in conftest, which build real GIFs via
  Pillow. Pillow also collapses identical animation frames into one, so an
  "animated" fixture needs visibly different frames to stay multi-frame.
- **Client capabilities load asynchronously; gate render on them.** `load()`
  awaits `capsLoaded` before drawing cards, because the per-card "describe"
  button depends on `capabilities.enrich`, which arrives via `pollJobs()`.
  Rendering before it resolves greys out a working button.
- ffmpeg conversion uses `scale='min(W,iw)'`, capping and never upscaling. Forcing
  a small clip up to a fixed width multiplied file size ~2x for zero gain.
- **Shortcuts are bare letters, so the typing guard is load-bearing.** The
  global keydown handler returns early when `isTyping(e.target)` or a modifier
  is held; without that, `j` lands in the search box instead of moving. For the
  same reason `#search` must not carry `autofocus`: it would swallow every
  shortcut on load, including the `?` the hint advertises.
- **Library-wide shortcuts click the real toolbar button** (`a`, `g`, `R`)
  rather than reimplementing it, so a shortcut can never drift from what the
  button does. Card actions do the same for delete and describe, which is how
  the confirm and the disabled state keep applying.
- **Selection is tracked by id, not index** (`selectedId`). A render can filter
  or reorder the wall, and the selection has to follow the GIF; an index would
  silently point at a different one. `targetCard()` falls back to the hovered
  card so mouse and keyboard habits coexist, and a selection pointing at a
  deleted GIF degrades to "nothing selected" rather than throwing.
- **Vertical movement reads the live column count** from
  `grid-template-columns`. The grid is `auto-fill`, so a hardcoded width makes
  `j` drift at every window size but the one it was written for.
- **Tagging is the primary filing mechanism; keep it cheap.** The chip input in
  `tagEditor()` is built around that: the field is always open (no click to
  reveal), a commit leaves it focused for the next tag, and **nothing in the
  editor calls `load()`**. A reload per tag refetches the library and rebuilds
  every card, throwing away scroll position and focus mid-file. Tag-bar counts
  are instead adjusted locally by `bumpTag()`, which a later `load()`
  reconciles against the server's authoritative numbers.
- **Separators are handled on `input`, not `keydown`.** Committing a tag on a
  space *keypress* silently ignores paste, autofill and IME input, none of which
  fire a keydown. Watching the value means pasting `funny reaction dog` files
  two tags and leaves `dog` editable. Keep Enter/Tab/arrows on keydown, where
  they belong; don't move space and comma back.
- **The suggestion list is the point, not decoration.** It is what stops
  `reaction` and `reactions` becoming two shelves with half the library each.
  Prefix matches rank above substring, then by count.
- **`.card` clips its children**, so the suggestion list only escapes because
  `.card.tagging` lifts `overflow` and raises `z-index` while a card is being
  tagged. Removing that class hook makes the dropdown a hairline again.
- **Don't gate features behind native `prompt()`/`alert()`/file dialogs.**
  Embedded and preview contexts silently suppress them, and browsers let users
  permanently disable dialogs, so a button that only calls `prompt()` looks dead.
  Grab URL uses an inline `#graburl` input, tag editing is inline
  `contenteditable`, and the file picker's `#file` input is `.visually-hidden`
  (not `display:none`, which some browsers refuse to open a picker for). Delete
  still uses `confirm()`, which is acceptable only because suppression fails
  safe: no confirmation means no deletion.
- **Previews load from the source URL, with `/api/preview` as fallback.** The
  scraped URLs carry live signatures and display fine; an earlier claim that the
  CDNs refuse cross-origin loads was a measurement artifact (the test URLs had
  been truncated, cutting the signature). What is real: firing all 155 previews
  at once gets the burst rate-limited, so the picker loads them on scroll via an
  IntersectionObserver and falls back to the proxy per image on error. Staging
  under `<root>/.staging` is cleared at startup and again after each import.
- **`grid-auto-rows: min-content` on `.pickgrid` is load-bearing.** With the
  default `auto`, rows in that definite-height scrolling grid resolved to ~8px
  and clipped every preview to a hairline, while the children still measured
  correctly. If the selection screen ever renders as thin lines, look at row
  sizing, not at the cells.
- **Mac detection must be case-insensitive.** `navigator.userAgentData.platform`
  returns `"macOS"` (lowercase m) while `navigator.platform` returns
  `"MacIntel"`, so a `/Mac/` regex matches the latter but misses the former, so on
  a modern Mac browser it wrongly reports non-Mac. Use `/mac/i`. The path-copy
  modifier is labelled "option" on Mac, "alt" elsewhere.

# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

What the parts mean here:

- **Patch** (`0.1.1`): fixes that change no interface.
- **Minor** (`0.2.0`): new features, and backwards-compatible changes to the
  HTTP API, the CLI, or the on-disk layout.
- **Major** (`1.0.0`): incompatible changes. While the version is `0.x` the API
  and library format may still change between minors, as SemVer allows; `1.0.0`
  is the point at which they stop.

The **library on disk is the compatibility surface that matters most**: a
`gifs/` folder, a `.trash/` folder, and `gifhole.db`. New database columns go
through `MIGRATIONS`, so an older library opens in a newer gifhole without
losing anything. Anything that would break that is a major change.

## [Unreleased]

### Added

### Fixed

- Frame sampling now reaches the end of an animation. The spacing put the last
  sample at `count/(count+1)` of the way through, so with the default of three
  the final quarter was never examined and a caption appearing only at the end
  was invisible to both OCR and Claude.
- OCR-gated code paths are tested against the function the app actually gates
  on. A rename left one test patching a helper nothing called, so it passed on
  macOS because Vision was genuinely present and failed wherever the stub was
  the only engine.
- The release workflow reads the changelog and its notes script from the
  default branch rather than from the tag being released. A tag is a snapshot
  and can predate both, which is exactly what happened with `v0.1.0`: the tag
  has no `scripts/` directory, so releasing it failed at the notes step.

### Added

- **Bookmarklet** offered from the library panel: drag it to the bookmarks
  bar, press it on any page with GIFs, and gifhole opens ready to import. It
  sends both what it finds in the rendered page and the page's address, and
  gifhole merges them, so JavaScript-rendered galleries (Imgur) work through
  the DOM while Reddit still benefits from server-side scraping. Navigates
  rather than calling the API, so it needs no CORS exception and no
  extension.
- **Optional shared token** (`--token`, `GIFHOLE_TOKEN`) for running gifhole
  somewhere other than the machine you are sitting at. Off by default, so a
  loopback install is unchanged. Covers every route including `/gifs/*`;
  accepted as a Bearer header, a cookie, or `?token=` once, which sets the
  cookie so images load.
- **Tesseract OCR** as the engine outside macOS, so burned-in text stays
  searchable on Linux instead of the feature being absent. Vision is still
  preferred where it exists, being better on stylised lettering.
- **Linux file clipboard** through `wl-copy` or `xclip`, putting a
  `text/uri-list` on the clipboard the way a file manager does, so a paste can
  stay animated there too. Reported unavailable without a graphical session
  rather than offering a button that fails.
- Dockerfile and compose file: run gifhole without a Python toolchain, with the
  library bind-mounted so the GIFs stay ordinary files on the host. Doubles as
  the Linux test environment, where the suite now passes with the macOS-only
  features absent.
- Continuous integration on every push and pull request: lint, format, a
  syntax check of the bundler-less frontend, the suite, and a boot with no
  optional dependencies installed.
- Contract tests for the couplings nothing else can see: element ids, job
  kinds, and capability names shared between the Python and the JavaScript,
  plus repo hygiene checks for stray credentials and for dashes.
- Pushing a `vX.Y.Z` tag now publishes the matching GitHub release, with the
  notes taken from this file so the two cannot drift. The workflow runs the
  tests and refuses a tag that disagrees with the packaged version.

## [0.1.0] - 2026-07-19

First tagged release.

### Added

- Local GIF library served at `127.0.0.1:8777` over a folder of `.gif` files,
  with search, tags, inline rename, and sort by newest / name / most copied.
- **Click to copy.** On macOS the local server puts the real file on the
  pasteboard, so a paste into Discord or Slack stays animated; browsers cannot
  put `image/gif` on the clipboard at all, so elsewhere it falls back to a still
  PNG. Shift-click copies the URL, alt-click copies the file path.
- **Automatic OCR** of burned-in text via macOS Vision, run in the background
  on every added GIF and searchable, so "nope" finds a GIF with NOPE in it
  without any tagging. Degrades to nothing off macOS.
- **Claude descriptions** (opt-in, `pip install 'gifhole[enrich]'`): a
  description, the meme's name, and tags, all searchable. Tags are pinned to the
  library's existing vocabulary by a schema `enum`, so bulk describing makes the
  vocabulary more consistent rather than filling it with near-synonyms. Runs per
  GIF, over a selection, or over a scope of the whole library, always with a
  count shown first because each GIF is one billable call.
- **Download by URL**: a direct `.gif` link, or a page to scrape, which opens a
  selection screen listing every GIF found. Giphy, Tenor and Reddit work
  (Reddit via `old.reddit.com`); see "known gaps" in the README for what
  doesn't. MP4/WebM sources are converted with ffmpeg when it is installed, and
  skipped with a note when it is not.
- **Adding**: drag and drop, paste a copied GIF file, drag a GIF straight off a
  web page (the browser hands over a URL, which is then downloaded), or the
  file picker. All file routes share one path, so all get the duplicate check.
- **Duplicate detection** on add: SHA-256 for the same file, and a perceptual
  hash of one frame for the same GIF resized or re-encoded. Matches are shown
  for confirmation with add-anyway or skip per file, never rejected outright.
  `/api/duplicates` finds copies already in the library.
- **Tagging built for volume**: an always-open field on every card with
  suggestions drawn from tags already in use, bulk tagging across a selection
  (`-tag` removes), and clickable tag chips with counts.
- **Keyboard for everything**, `?` for the map: `hjkl` and arrows to move,
  `Enter`/`c`/`u`/`p` to copy, `t` tags, `r` renames, `e` describes, `x` trashes,
  `z` undoes, `Space` selects, `T` opens the trash.
- **Recoverable removal**: deleting moves files to `<root>/.trash/`, `z` undoes
  the last removal, and the trash panel restores or permanently deletes.
  Emptying the trash is the only operation in gifhole that destroys anything.
- **Library panel** collecting the whole-library actions: scoped describe with
  counts, rescan, duplicates, trash, and clear (which moves everything to the
  trash, so it is still undoable). A queued run can be stopped from the job
  strip.
- **`gifhole move DEST`** relocates a library. Files only: no row holds a path,
  so nothing needs rewriting.
- **Selectable dot-com-era skins**, remembered across visits: memepool
  (default), fark, zombo, webvan, pets.com, altavista, and a tongue-in-cheek
  linkedin. Driven entirely by CSS variables and a `data-theme` attribute.
- Background job queue with live status, `--reload` for development, and a
  Homebrew formula with `brew services` support.

### Security

- The SSRF guard is re-applied on **every redirect hop**. A public URL that
  redirected to loopback was previously followed and its body returned, making
  `/api/preview` a full-read SSRF against local services.
- Mutating routes refuse cross-site requests, so a visited page cannot trigger
  billable enrichment or write to the library.
- Trash filenames arrive from the client, so they resolve through a guard that
  refuses anything landing outside `.trash/`.

### Fixed

- Importing the app module no longer creates a library, starts a worker, or
  runs OCR over the real `~/.gifhole` as a side effect.
- A failed OCR is recorded as a failed job instead of being stored as empty
  text, which had marked the GIF as read and excluded it from future retries.
- Deleting two same-named GIFs within one second no longer overwrites the first
  in `.trash`.
- `Rescan` finds files with uppercase extensions such as `FOO.GIF`.
- API failures surface instead of passing silently: a server running older code
  answers new routes with 404, which used to read as an empty result and showed
  a convincing but false empty trash.

[Unreleased]: https://github.com/kcm/gifhole/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kcm/gifhole/releases/tag/v0.1.0

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

### Changed

- The Python floor is 3.11, down from 3.13. Nothing in gifhole needed 3.13,
  and requiring it meant that Debian 12 and Ubuntu 22.04 users had to install
  a second Python before they could install gifhole at all. Resolved
  dependency versions are unchanged. CI now runs the floor rather than the
  newest release, since development already exercises the newest every day.

### Added

- A process console, toggled with ` or ~ like a GUI editor's terminal. Hidden
  by default and remembered per browser, it is the detailed view the ambient
  rail is not: timestamped sub-steps as they happen ("reading text: cat.gif",
  "cat.gif: asking claude-opus-4-8", "cat.gif: tagged funny, dog"). Backed by
  an in-memory log any server path can emit into, read over HTTP by a cursor,
  so it tails live without replaying and reusable for future long operations.
  A costly describe-all is stopped from here too. It replaces the old job
  strip rather than sitting alongside it. Every way a GIF enters the library
  logs a line, including a dropped file and a rejected duplicate, which used
  to happen silently.
- Cross-engine browser tests (`browser/run`): the UI is exercised in chromium,
  firefox and webkit against the real Linux container, not just in the
  author's Mac browser. They assert that the console stays silent, because
  every UI bug this project has shipped surfaced first as an uncaught
  exception and a control that quietly did nothing.

### Added

- Startup prints the optional capabilities (`reading:` for OCR, `enrich:` for
  Claude describe), so a launch says `enrich: ready` or `enrich: off (no API
  key...)` up front. Both were previously invisible when missing: a disabled
  button, an OCR that silently did not run.
- Describe replaces the description and merges tags: a GIF has one description,
  so a re-describe overwrites a bad earlier one, while tags are added to and
  never removed, matching how bulk tagging works. A per-card undo (a small ↶
  shown only when the describe changed something) restores the previous
  description and tags together, so an unwanted describe can be taken back
  whole.
- Per-entry OCR control: clicking a GIF's burned-in text opens a small re-run /
  delete menu. Re-run re-reads just that GIF (picking up the OCR filter), delete
  clears its text and marks it read so a Rescan will not bring the noise back. A
  GIF with no text yet shows a faint "read text" affordance that offers re-run.
- A "Re-read text" maintenance action in the library panel. Rescan only reads
  GIFs never read before, so after the OCR itself improves there was no way to
  re-apply it to what was already in the library. This re-reads every GIF's
  burned-in text, replacing what is stored. Free and local, so it is grouped
  with the other no-cost maintenance actions, not with the paid describe.

### Changed

- The import picker pre-selects only the main GIF when it came from the
  bookmarklet. A single GIF's page hands over the hero plus its size variants;
  they are all imported but only the hero is ticked, so you are not filing five
  copies by reflex. Grab URL still ticks the whole page. Import and the select
  buttons disarm when they would do nothing.
- The skin picker moved from the footer into the Library panel (a new
  "Appearance" section), and the panel is tidied now that it holds more.
- Removed the tag cloud above the wall. Tag filtering still works (click a tag
  chip on any card, or type the tag in search); the cloud was prime real estate
  for a secondary navigation mode.

### Fixed

- A "possible dupes: N" prompt next to the Library button, so duplicates
  surface on their own instead of only when you go looking. A background scan
  refreshes the count at startup and after the library changes; clicking it
  opens the duplicates review.
- Duplicate detection now compares several frames per GIF instead of one, so two
  encodes of the same GIF at different lengths (e.g. 54 frames vs 29) are caught.
  The single-frame hash sampled "a third of the way in", which landed on
  different moments when the lengths differed and missed real duplicates. Run
  "Find duplicates" once after upgrading; it re-hashes the library as it scans.
- Card polish (a design-review pass): the per-card delete "x" is muted instead
  of bold blue so a wall of them stops competing with the GIFs, the description
  is roman while the OCR quote stays italic so the two stop reading as one grey
  block, and the meta-row checkbox sits on the text midline.
- The card's lower half no longer collapses into a mess when tags, OCR text and
  a description are all present. The burned-in text, the description and the
  describe/undo actions each get their own line now, so a description wraps
  full-width instead of into a tall narrow ribbon beside "read text".
- The console logs the OCR text itself, and the raw read when the scoreboard
  filter changed it, instead of a meaningless "1 line" (the text is one joined
  line, so the count was always 1). You can now see exactly what was read and
  what the cleanup dropped.
- Deleting a GIF's OCR text updates the card immediately, in place, instead of
  relying on a grid reload that could lag or, if the current search matched the
  text just removed, drop the card.
- OCR no longer stores scoreboard and timer noise as searchable text. Vision
  reads a burned-in match clock or a channel bug with high confidence, so the
  confidence threshold never caught them; a lexical filter now drops lines that
  read as a HUD (bare numbers, clocks, single-letter fragments) while keeping
  real captions, even mangled ones. A football clip that stored
  "89 73:29 DOR 0 L RMA ES ... VAMOS" now stores just the caption.
- The library panel opens. `body` was scoped to the `try` and read outside it,
  so the panel threw and stayed hidden, taking bulk describe, find duplicates,
  trash, clear-the-library and the bookmarklet with it.
- Setting a token via `docker compose` works. `GIFHOLE_TOKEN` was commented out
  in `compose.yaml`, so the README's own instructions for exposing gifhole
  produced a server with no authentication at all.
- An explicit `?token=` now outranks a cookie already held for that host.
  Cookies ignore the port, so an owner's writer cookie answered for every
  gifhole on the machine and their own guest link could not be checked.
- A malformed `#add=` fragment is cleared from the address bar and a `null`
  payload no longer throws. It used to re-fire the same complaint on every
  reload, and a repeat press of the bookmarklet fired no `hashchange` at all.
- Half-configured access control refuses to start. A read token or public
  reads without a write token used to warn and serve everything, which looks
  like access control and is not.
- `/openapi.json` is no longer served. The interactive docs were already off
  but the schema was not, so `--public-reads` would have handed anyone a map
  of every write route.
- Nothing is printed, opened, or bound until the configuration is known to be
  serviceable. An invalid one announced "serving:" and armed the browser first.
- Port conflicts are handled before anything is printed or opened. It used to
  report "serving:" and open a browser after the bind had already failed, and
  exit 0, so a supervisor saw a clean start. Now an explicit `--port` that is
  taken refuses to start, while a busy default moves to the next free port and
  says so.
- Startup lines are flushed, so they appear immediately under a supervisor
  (`brew services`, Docker) where stdout is a pipe and block-buffered.
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

- `scripts/gifhole-pick`, a macOS launcher: search the library and copy a GIF
  onto the clipboard, animation intact, without leaving what you are typing in.
  Native AppleScript picker, no third-party launcher, bindable to a hotkey with
  Shortcuts.app or Automator.
- Search filter words `untagged`, `undescribed` and `untitled`, which combine
  with ordinary search terms. Filing needs a way to see what is left, and a
  count in a panel is not something you can type. Idea borrowed from gifdex.
- **Bookmarklet** offered from the library panel: drag it to the bookmarks
  bar, press it on any page with GIFs, and gifhole opens ready to import. It
  sends both what it finds in the rendered page and the page's address, and
  gifhole merges them, so JavaScript-rendered galleries (Imgur) work through
  the DOM while Reddit still benefits from server-side scraping. Navigates
  rather than calling the API, so it needs no CORS exception and no
  extension.
- **`--public-reads`**: let anyone browse without a token while writes stay
  behind yours. `/api/preview` is excluded, since it fetches a URL of the
  caller's choosing and would otherwise be an open proxy.
- **Read-only token** (`--read-token`, `GIFHOLE_READ_TOKEN`) so a library can
  be shared without handing over the ability to change it. Anything that is not
  a read is refused, and the interface hides the controls a guest cannot use.
  Requires the write token; alone it is ignored with a warning, since it would
  otherwise look like access control while leaving writes open.
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

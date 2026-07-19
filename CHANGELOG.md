# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- The SSRF guard is now re-applied on every redirect hop. A public URL that
  redirected to loopback was previously followed and its body returned, making
  `/api/preview` a full-read SSRF against local services.
- Mutating routes refuse cross-site requests, so a visited page can no longer
  trigger billable enrichment or write to the library.
- Importing the app module no longer creates a library, starts a worker, or
  runs OCR over the real `~/.gifhole` as a side effect.
- A failed OCR is recorded as a failed job instead of being stored as empty
  text, which had marked the GIF as read and excluded it from future retries.
- Deleting two same-named GIFs within one second no longer overwrites the first
  in `.trash`.
- `Rescan` now finds files with uppercase extensions such as `FOO.GIF`.

### Changed

- Importing a page now opens a selection screen: every GIF found is listed with
  a checkbox (all ticked by default) plus select all / select none, and only
  the ticked ones download. Previews load from the source on scroll, with a
  server-side proxy as a per-image fallback.
- Reddit page URLs now work: they resolve via `old.reddit.com` (www serves a
  JS shell and the `.json` API 403s bots) and collect every GIF on the page,
  comments included.
- Homebrew formula with `brew services` support, to run gifhole in the
  background and at login on macOS.
- Renamed the project from `gifbox` to `gifhole`.
- *Grab URL* now opens an inline field instead of a browser `prompt()`, which
  some contexts suppress; the file picker input is visually-hidden rather than
  `display:none` so the picker opens reliably.
- The path-copy shortcut is now labelled "option" on macOS, "alt" elsewhere.
- **Selectable dot-com-era skins**, chosen from a footer picker and remembered
  across visits: memepool (default), fark, zombo (with animated rings), webvan,
  pets.com, altavista, and a tongue-in-cheek linkedin that even rebrands the
  masthead. Each carries its own period tagline. Driven entirely by CSS
  variables + a `data-theme` attribute; no markup or behavior changes.

### Added

- **Automatic OCR** of burned-in text via macOS Vision, run in the background on
  every added GIF. The text is searchable, so "nope" finds a GIF with NOPE in it
  without any tagging. Degrades to nothing off macOS.
- **Claude enrichment** (opt-in, `pip install 'gifhole[enrich]'`): a per-GIF
  "describe" button that adds a one-line description, identifies the meme, and
  suggests tags, all searchable. Requires an Anthropic API key.
- **Download by URL**: a direct `.gif` link, or any page to scrape. Handles
  Giphy/Tenor/Reddit/Imgur (which serve MP4) via ffmpeg conversion; pasting a
  URL anywhere triggers it. Video is skipped gracefully when ffmpeg is absent.
- Background job queue with live status in the UI header.
- Local GIF library served at `127.0.0.1:8777` over a folder of `.gif` files.
- Click to copy a GIF as a still PNG; shift-click copies its URL; alt-click
  copies its file path.
- Drag-and-drop upload, plus *Rescan* to index files added to the folder by hand.
- Search across filename, title, and tags; clickable tag chips with counts.
- Inline rename, tag editing, and sort by newest / name / most copied.
- Delete moves files to `<root>/.trash/` rather than unlinking them.

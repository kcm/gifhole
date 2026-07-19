# gifhole

A local GIF library, like giphy.com, except it is your folder and it runs on
your machine.

```
uv run gifhole
```

Opens `http://127.0.0.1:8777/`. GIFs live in `~/.gifhole/gifs/` by default; point
it somewhere else with `--root ~/Pictures/gifs`.

## Using it

- **click** a GIF: copies the image, ready to paste into Slack, Messages, docs
- **shift-click**: copies its `http://127.0.0.1:8777/...` URL
- **alt-click** (option-click on a Mac): copies its file path
- **drag GIFs onto the window** (or press *Add GIFs*) to add them
- **paste a URL**, or press *Grab URL* and type one, to download from a `.gif`
  link or scrape a page. Giphy, Tenor, Reddit, and Imgur included
- **`/`** focuses search, **Esc** clears search and tag filters
- click a name to rename it; click *tags* to tag it; click a tag chip to filter
- pick a **skin** from the footer: memepool, fark, zombo, webvan, pets.com,
  altavista, or a straight-faced linkedin; your choice is remembered

Dropping files into the `gifs/` folder by hand works too; press *Rescan* to
pick them up.

## Finding GIFs by what's in them

Every added GIF is scanned for **burned-in text** (on macOS, via the Vision
framework, so no key and no network). So searching `nope` finds the GIF with NOPE
across it, even if you never tagged it. This runs automatically in the
background; the header shows progress.

For descriptions and meme identification, press **describe** on a card. That
sends a few frames to Claude and comes back with a one-line description, the
meme's name if it's a known one, and suggested tags, all searchable. It's
opt-in and needs an API key:

```
uv sync --extra enrich          # installs the anthropic SDK
export ANTHROPIC_API_KEY=...     # or use `ant auth login`
```

Without it, everything else still works; the *describe* button just stays
disabled.

## Downloading

*Grab URL* (or pasting a link) accepts either a direct `.gif` or a page to
scrape. A direct link imports straight away. A page opens a **selection
screen** showing every GIF found on it, all ticked, with select all / select
none, so you keep only what you want. Previews load from the source as you
scroll, falling back to the server if one fails.

Most sites (Giphy, Tenor, Reddit, Imgur) serve MP4/WebM rather than GIF; those
are converted with **ffmpeg** if it's installed (`brew install ffmpeg`).
Without ffmpeg, direct GIFs still download and videos are skipped with a note
rather than failing the batch. The server refuses `file://` and
private-network addresses.

### One caveat on copying

Browsers only accept a few image formats on the clipboard, and GIF is not one of
them. A plain click therefore pastes the **first frame** as a still PNG. When
you need the animation to survive, shift-click to copy the URL instead, which
stays animated anywhere that can reach your machine.

## Options

```
gifhole --root DIR    library location   (default ~/.gifhole)
       --port N      port               (default 8777)
       --host ADDR   bind address       (default 127.0.0.1, loopback only)
       --no-open     do not open a browser
```

Deleting a GIF moves it to `<root>/.trash/`; nothing is erased from disk.

## Homebrew, and running it at login (macOS)

[`packaging/homebrew/gifhole.rb`](packaging/homebrew/gifhole.rb) is a formula
with `brew services` support, so gifhole can run in the background and start
automatically at login:

```
brew services start gifhole    # run now, and at every login
brew services stop  gifhole
brew services info  gifhole
```

It serves on `127.0.0.1:8777` and logs to `$(brew --prefix)/var/log/gifhole.log`.

The formula needs two things filled in before it will install: a tagged release
(or `--HEAD`) for `url`/`sha256`, and pinned dependency `resource` blocks, which
Homebrew requires because it builds in a network sandbox:

```
brew update-python-resources packaging/homebrew/gifhole.rb
```

The file's header comment walks through both. ffmpeg is intentionally not a
dependency, because gifhole runs fine without it and skips video sources; run
`brew install ffmpeg` to enable video-to-GIF conversion.

## Development

```
uv sync
uv run pytest
uv run ruff check .
```

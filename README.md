# gifhole

A local GIF library, like giphy.com, except it is your folder and it runs on
your machine.

```
uv run gifhole
```

Opens `http://127.0.0.1:8777/`. GIFs live in `~/.gifhole/gifs/` by default; point
it somewhere else with `--root ~/Pictures/gifs`.

## Using it

- **click** a GIF: copies it, ready to paste into Discord, Slack, Messages, docs.
  On macOS the animation survives; see [copying](#on-copying-and-keeping-the-animation)
- **shift-click**: copies its `http://127.0.0.1:8777/...` URL
- **alt-click** (option-click on a Mac): copies its file path
- **drag GIFs onto the window** (or press *Add GIFs*) to add them
- **paste a URL**, or press *Grab URL* and type one, to download from a `.gif`
  link or scrape a page. Giphy, Tenor, Reddit, and Imgur included
- click a name to rename it
- **press `?`** for the full keyboard map, or click *keyboard shortcuts*
- pick a **skin** from the footer: memepool, fark, zombo, webvan, pets.com,
  altavista, or a straight-faced linkedin; your choice is remembered

Dropping files into the `gifs/` folder by hand works too; press *Rescan* to
pick them up.

## Keyboard

Everything is reachable without the mouse. `?` shows this list in the app.

```
h j k l   move between GIFs      Enter c  copy the current GIF
← ↓ ↑ →   the same               u        copy its URL
Home End  first, last            p        copy its file path
Esc       back out one layer     t        edit tags
                                 r        rename
/   search        s  change sort e        describe with Claude
a   add GIFs      R  rescan      x        move to trash
g   grab a URL    ?  this list
```

Keys act on the **selected** GIF (outlined), falling back to whatever the
pointer is over, so keyboard and mouse habits both work without a mode switch.
Movement follows the grid, so `j` drops a whole row however wide the window is.
`Esc` backs out one layer at a time: suggestions, then the field, then the
selection, then search and tag filters.

The search box is deliberately **not** focused on load, or it would swallow
every shortcut. `/` is one keystroke away.

## Tagging

Tags are the filing system, so adding one is meant to cost nothing. Every card
carries an always-open field: click it, or hover the card and press **`t`**.

- type and press **Enter**, **Tab**, **space** or **comma** to file a tag; the
  field stays focused for the next one
- **suggestions come from tags you already use**, ranked by how often, so you
  reuse *reaction* instead of quietly inventing *reactions* and splitting the
  shelf. Arrow keys pick, Enter accepts
- **paste** `funny reaction dog` to get three tags at once
- **backspace** on an empty field removes the last tag; each tag also has an
  **x**
- **click a tag** on a card to see everything else filed under it

Nothing here reloads the page, so you can work down a wall of GIFs without
losing your place. Tag counts in the header update as you go.

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

### On copying, and keeping the animation

Browsers only accept a few image formats on the clipboard, and GIF is not one of
them, so a web page can only ever hand over a still PNG of the first frame.

On **macOS**, gifhole gets around this: a plain click asks the local server to
put the actual `.gif` **file** on the pasteboard, exactly as Finder's Copy does.
Discord, Slack and friends then upload the real file and the animation survives.
This needs the server running, which it is if you're looking at the page.

Elsewhere a plain click falls back to the still PNG. Shift-click copies the URL
instead, which stays animated anywhere that can reach your machine.

## Options

```
gifhole --root DIR    library location   (default ~/.gifhole)
       --port N      port               (default 8777)
       --host ADDR   bind address       (default 127.0.0.1, loopback only)
       --no-open     do not open a browser
       --reload      restart on source changes (development)
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
uv run gifhole --reload        # picks up source edits without a restart
```

`--reload` watches the package's `.py` files and restarts the server when one
changes, so you never test against code you already edited. Static assets are
read from disk per request and need no restart, only a browser refresh.

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
- **drag GIFs onto the window**, **paste one** (copy the file in Finder, then
  press paste anywhere on the page), or press *Add GIFs*. All three take the
  same path, including the duplicate check
- **drag a GIF straight off a web page** (Giphy, Tenor, a search results page):
  the browser hands over a URL rather than a file, so it is downloaded like a
  *Grab URL*. Where the drag carries a title, that becomes the filename, since
  every file on Giphy is otherwise called `giphy.gif`
- **paste a URL**, or press *Grab URL* and type one, to download from a `.gif`
  link or scrape a page. Giphy, Tenor and Reddit work; see
  [known gaps](#known-gaps) for what doesn't
- click a name to rename it
- **press `?`** for the full keyboard map, or click *keyboard shortcuts*
- pick a **skin** from the footer: memepool, fark, zombo, webvan, pets.com,
  altavista, or a straight-faced linkedin; your choice is remembered

Dropping files into the `gifs/` folder by hand works too; press *Rescan* to
pick them up.

## Adding from any page

*Library* offers a **bookmarklet**: drag *Add to gifhole* to your bookmarks
bar, then press it on any page with GIFs. It opens gifhole with that page ready
to import, so a page gives you the selection screen and a direct link imports
straight away.

It navigates rather than calling the API. A bookmarklet runs in the visited
page's origin, so a request from there would be cross-origin and refused by the
same check that stops a random site driving your library. Opening a URL is a
plain page load and needs no exception. The bookmark is built from whatever
address you are using, so it works on a different port or a server, and if a
token is set it relies on the cookie from your last visit.

It only sends the page's address, so it inherits whatever the server-side
scraper can do: Giphy, Tenor and Reddit yes, Imgur no. A version that scanned
the rendered page instead would fix Imgur, at the cost of a much longer
bookmarklet.

## Duplicates

Adding a GIF you already have opens a review panel showing the incoming file
beside what it matched, with *Add anyway* or *Skip* per file. Nothing is
written until you say so, and nothing is ever rejected outright.

Two checks run. **Exact** matches compare a SHA-256 of the file, which catches
re-scraping a page or re-downloading a link. **Near** matches compare a
perceptual hash of one frame, which catches the same GIF re-encoded, resized,
or re-hosted, where the bytes differ but the picture does not.

Comparison is always on a **single frame**, never across the animation: two
cuts of the same scene at different lengths should still match, and
frame-by-frame comparison would cost far more for a worse answer. Frame 0 is
not the one used, though, because plenty of GIFs open on a fade or a title
card; it takes one about a third in and falls back to other positions if that
frame turns out to be flat. Hashing costs about 0.3 ms per GIF, so backfilling
a few hundred is instant.

The threshold was measured rather than guessed: across 30 resized pairs and 90
unrelated pairs, the same picture at a different size scored at most 13 and
different pictures never below 19, so the cutoff sits at 12. It leans towards
flagging, because a false positive costs one click and a miss puts a duplicate
in your library permanently. A GIF with no structure at all (a solid colour, a
plain title card) gets no perceptual hash, since every flat image hashes alike
and they would otherwise all match each other.

`GET /api/duplicates` reports copies already sitting in the library, and
backfills hashes for anything added before this existed.

Not using the `imagededup` package, despite it being the obvious reference:
installing it pulls in torch, torchvision, scikit-learn, scipy and matplotlib,
several gigabytes, almost all of it for CNN methods gifhole would not use.
`dedupe.py` implements the same published hash in about forty lines with
Pillow alone.

## Keyboard

Everything is reachable without the mouse. `?` shows this list in the app.

```
h j k l   move between GIFs      Enter c  copy the current GIF
← ↓ ↑ →   the same               u        copy its URL
Home End  first, last            p        copy its file path
Esc       back out one layer     t        edit tags
                                 r        rename
/   search        s  change sort e        describe with Claude
a   add GIFs      R  rescan
g   grab a URL    ?  this list   Space v  select for bulk actions
                                 A        select all, or none
                                 t        tag everything selected
                                 e        describe everything selected
                                 x        move selected to trash
                                 z        undo the last removal
                                 T        open the trash
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

### Tagging a batch

Filing a freshly scraped thread one GIF at a time is the slow way. Tick several
with `Space` (or the checkbox on each card), then press `t`: the same field
appears in the bulk bar, with the same suggestions, and applies to everything
selected at once.

- **`-tag`** removes instead of adding, which is the only way to take a tag off
  a batch. `funny -todo` does both in one go
- adding is a **union**, so filing 40 GIFs under *reaction* never wipes what
  each was already tagged with
- the selection **stays** after applying, so tagging the same batch *reaction*
  then *meme* is two keystrokes apart, not two rounds of re-selecting
- the toast reports what actually changed (`+funny on 12 of 40`), so re-applying
  a tag most of them already had reads as `0 of 40` rather than pretending

## Finding GIFs by what's in them

Every added GIF is scanned for **burned-in text**, so searching `nope` finds
the GIF with NOPE across it even if you never tagged it. It runs automatically
in the background, needs no key and no network, and the header shows progress.

Two engines. **macOS Vision** where it exists, and **Tesseract** everywhere
else (`apt install tesseract-ocr`, already in the Docker image).

**Vision is substantially better at this, and the gap is the whole reason it is
preferred.** Meme text is warped, stylised, and laid over busy pictures, which
is close to the worst case for a classical OCR engine. On a clean test caption
Vision returned `NOPE NOT TODAY` where Tesseract gave `NOPE'NOT TODAY`; on real
memes expect Tesseract to do noticeably worse than that, not better.
[FindThatMeme](https://findthatmeme.com/blog/2023/01/08/image-stacks-and-iphone-racks-building-an-internet-scale-meme-search-engine-Qzrz7V6T.html)
hit the same wall at seventeen million memes and ended up running a physical
rack of second-hand iPhones to get at Vision, which is a strong signal about
the size of the difference.

So: Tesseract makes burned-in text searchable on Linux rather than the feature
being absent, and search is substring-based so partial reads still match. Treat
it as a fallback, not a replacement.

For descriptions and meme identification, press **describe** on a card. That
sends a few frames to Claude and comes back with a one-line description, the
meme's name if it's a known one, and suggested tags, all searchable. It's
opt-in and needs an API key:

```
uv sync --extra enrich          # installs the anthropic SDK
export ANTHROPIC_API_KEY=...     # or use `ant auth login`
```

Without it, everything else still works; the *describe* button just stays
disabled and says why.

Every card carries an editable **description** under the tags, whether or not
Claude wrote it: click it, type, press Enter. Burned-in text found by OCR is
shown separately above it, in quotes, and is not editable, because that is a
fact about the picture rather than a note about it. Both are searchable.
Editing a description by hand does not mark the GIF as described, so a later
batch will still offer to describe it.

### Describing a batch

Select GIFs and press **`e`** (or *Describe* in the bulk bar) to run the same
thing over all of them. It asks first and tells you how many, because each GIF
is one billable API call, and it skips any you've already described so
re-running a partly-done batch only pays for the rest.

**Tags stay in a controlled vocabulary.** The request pins the model's tag
choice to the tags your library already uses, as a schema `enum`, so an
off-vocabulary tag cannot come back at all. It may add at most two genuinely
new tags per GIF, and only when nothing existing fits. Left unconstrained a
model invents a fresh near-synonym for every GIF (*laughing*, *laughter*,
*lol*, *hilarious*), which is the same shelf-splitting that autocomplete
prevents for you, at machine speed.

The vocabulary is read as each GIF is processed, not when the batch is queued,
so a long run gets more consistent as it goes: tags the early GIFs introduce
are on offer to the later ones.

Identified meme names go into the **description** rather than the tags, since
splitting *distracted boyfriend* into tags would shed *distracted* and
*boyfriend* into your vocabulary. Descriptions are searchable either way.

## Downloading

*Grab URL* (or pasting a link) accepts either a direct `.gif` or a page to
scrape. A direct link imports straight away. A page opens a **selection
screen** showing every GIF found on it, all ticked, with select all / select
none, so you keep only what you want. Previews load from the source as you
scroll, falling back to the server if one fails.

Most sites (Giphy, Tenor, Reddit) serve MP4/WebM rather than GIF; those are
converted with **ffmpeg** if it's installed (`brew install ffmpeg`).
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

On **Linux** the same trick works through `wl-copy` (Wayland) or `xclip` (X11),
which put a `text/uri-list` on the clipboard, exactly as a file manager does
when you copy a file. Install one of those and it behaves like macOS. It needs
a graphical session, so it is unavailable in the container, and gifhole reports
it as unavailable rather than offering a button that always fails.

Anywhere else a plain click falls back to the still PNG. Shift-click copies the
URL instead, which stays animated anywhere that can reach your machine.

## Options

```
gifhole --root DIR    library location   (default ~/.gifhole)
       --port N      port               (default 8777)
       --host ADDR   bind address       (default 127.0.0.1, loopback only)
       --no-open     do not open a browser
       --reload      restart on source changes (development)
       --token TOKEN require a token on every request (or GIFHOLE_TOKEN)
```

## Running it on a server

gifhole binds to loopback and has **no authentication by default**, which is
right for the machine you are sitting at and wrong the moment it is reachable
by anything else. There is nothing clever protecting it: any client that can
open the port can read the library, clear it, and empty the trash permanently.
The cross-site check only stops a *browser* being used as the weapon.

If you expose it, set a token:

```
gifhole --host 0.0.0.0 --token "$(openssl rand -hex 24)"
# or
GIFHOLE_TOKEN=... docker compose up -d
```

Then visit `http://host:8777/?token=...` once. The token is remembered in a
cookie, which is also what lets the GIF images load: an `<img>` tag cannot
carry an `Authorization` header. Scripts can use `Authorization: Bearer ...`
instead. Every route is covered, including `/gifs/*`, since those files are
the data.

The token is a shared secret over plain HTTP, so it is only as private as the
network. For anything beyond a trusted LAN, put it behind a reverse proxy with
TLS, or on a private network like Tailscale.

## Moving the library

```
gifhole --root ~/.gifhole move ~/Pictures/gifs
```

Files only. Nothing in the database is a path (rows store a bare filename and
the root is given at runtime), so relocating is a directory move rather than a
migration, and titles, tags, descriptions and the trash all come with it.

It refuses to move onto a non-empty directory, into its own subtree, or out of
a folder that isn't a library, so a mistyped `--root` can't relocate the wrong
thing. Afterwards it prints the `GIFHOLE_ROOT` to set, because nothing else
points at the new location and the next bare run would otherwise start an empty
library at the old path. **Stop the server first**; moving a library out from
under a running one is not handled.

## The library panel

*Library* in the toolbar collects everything that acts on the whole collection
rather than one GIF, so a costly action and a destructive one are not sitting a
click away from everyday buttons.

**Describe with Claude** is the expensive one, so it is scoped and counted
before it runs. Pick *missing tags or description*, *missing a description*,
*missing tags*, or *everything, redo what is done*; the panel shows exactly how
many GIFs that covers, and the confirm repeats the number, because it is one
API call each.

A running batch can be **stopped**: while jobs are queued the strip below the
toolbar shows how many are waiting and a *stop the rest* button. It cancels
everything not yet started; the one call already in flight finishes, so it
stops what you have not paid for rather than pretending to halt instantly.
Cancelling a 12-GIF run four seconds in avoided 10 of the 12 calls.

Scoping is the point: on a library you have been adding to for months, the
useful run is usually "the ones I never got round to", not all of it. Tags stay
in your existing vocabulary either way, so a big run makes the library more
consistent rather than noisier. Describing 8 test GIFs added exactly two tags
across all of them.

**Maintenance** (rescan the folder, find duplicates, open the trash) is local
and costs nothing. **Danger** holds *clear the library*, which moves everything
to the trash and so is still undoable.

## Removing things

Deleting is always a two-step affair, because the first step is undoable and
only the second is not.

**Removing** moves files to `<root>/.trash/`. Press `x` (or the card's `x`), or
tick several with `Space` and remove them together. A single removal doesn't
ask, because `z` takes it straight back; a batch does. *clear the library* in
the footer moves everything to the trash at once.

**The trash** (`T`, or via *Library*) lists what you removed, with sizes and
when. Restore puts a GIF back under its original name, or under `name-2.gif` if
you've since reused the name.

**Deleting from the trash is the only thing in gifhole that destroys
anything.** Those buttons arm rather than fire: the first press turns *Empty
the trash* into *Really? No undo*, the second does it. No dialog stacks on top
of the panel, and leaving or reopening the panel disarms it.

Trashed files are plain `.gif`s in a plain folder, so you can also just go
looking with Finder.

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

## Docker

Run it without a Python toolchain:

```
docker compose up -d
open http://127.0.0.1:8777
```

The library is a **bind mount** (`./library`), not a named volume, on purpose:
the whole premise is that your GIFs stay ordinary files in a folder you can
open in Finder, and a named volume would bury them inside Docker. Files added
through the containerised app are plain files on the host, and they outlive the
container.

The port is published to `127.0.0.1` rather than `0.0.0.0`. gifhole has no
authentication, so it should not be reachable from your network. The `0.0.0.0`
inside the container is not the same thing: the container's namespace is the
boundary and the mapping decides what is actually exposed.

ffmpeg and Tesseract are both in the image, so URL imports of MP4 sources work
and burned-in text is still searchable. The **file clipboard is unavailable**
in the container: it needs a graphical session and there isn't one, so a plain
click falls back to copying a still.

### Testing on Linux

```
docker run --rm -v "$PWD":/src:ro python:3.13-slim sh -c \
  'cp -r /src /work && cd /work && rm -rf .venv .git &&
   pip install -q uv && uv sync -q && uv run pytest -q'
```

Copied in rather than mounted read-write: running `uv sync` against a mounted
source tree replaces the host's `.venv` with Linux binaries.

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

## Known gaps

What has and hasn't been exercised, so you know where you are on your own. Each
is a reasonable thing to pick up; none is load-bearing for the rest.

- **Imgur pages do not work.** It serves a 5 KB JavaScript shell with no
  Open Graph tags and no media links, so there is nothing to scrape without
  their API or a headless browser. Direct `i.imgur.com/*.gif` links are fine,
  as any direct link is. Reddit needed its own workaround (`old.reddit.com`)
  for the same reason; Imgur has no equivalent.
- **Giphy search pages yield only the first handful.** They lazy-load, so a
  scrape sees roughly six. Individual GIF pages and direct media links are
  unaffected.
- **Linux is covered by the container**: the suite passes there, OCR runs on
  Tesseract, and the clipboard was verified against a virtual X display.
  **Windows is untested.** The browser side has only been exercised in Chrome
  on macOS.
- **Tesseract's real-world accuracy on memes is unmeasured here.** It was
  tested on clean synthetic captions, not on the warped, stylised text it is
  worst at. Assume it is meaningfully behind Vision.
- **The Linux clipboard is verified only as far as the clipboard.** The right
  `text/uri-list` lands on it; whether your particular chat client turns that
  into a file upload has not been tested end to end.
- **Firefox has had one path verified** (dragging a GIF off a page, using its
  `text/x-moz-url`). Safari is untested; it supplies no title with a drag, so
  imports there will be named `download.gif`.
- **URL imports skip the duplicate check.** Dropping the same Giphy GIF twice
  adds it twice; only file adds are deduped.
- **A running server does not notice a library move.** Stop it first.

Versions follow [SemVer](https://semver.org/): see
[CHANGELOG.md](CHANGELOG.md) for what counts as a patch, a minor, and a major
here. Releasing is one command, since pushing the tag publishes the GitHub
release from the changelog entry:

```
# bump version in pyproject.toml, add the CHANGELOG section, then
git tag -a v0.2.0 -m "gifhole 0.2.0" && git push origin v0.2.0
``` The library on disk is the compatibility surface that matters, and new
database columns are migrated on open, so an older library keeps working.

## License

MIT. See [LICENSE](LICENSE).

"""SQLite-backed metadata for the GIF library.

The files on disk are the source of truth: the database only annotates them
(title, tags, copy counts). A rescan reconciles the two, so deleting the
database loses annotations but never GIFs.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from gifhole import dedupe

SCHEMA = """
CREATE TABLE IF NOT EXISTS gifs (
    id        INTEGER PRIMARY KEY,
    filename  TEXT NOT NULL UNIQUE,
    title     TEXT NOT NULL DEFAULT '',
    tags      TEXT NOT NULL DEFAULT '',
    width     INTEGER NOT NULL DEFAULT 0,
    height    INTEGER NOT NULL DEFAULT 0,
    bytes     INTEGER NOT NULL DEFAULT 0,
    added_at  REAL NOT NULL DEFAULT 0,
    copies    INTEGER NOT NULL DEFAULT 0,
    favorite  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dismissed_duplicates (
    gif1_id    INTEGER NOT NULL,
    gif2_id    INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (gif1_id, gif2_id)
);
CREATE TABLE IF NOT EXISTS confuser_hashes (
    phash_val  TEXT PRIMARY KEY,
    hit_count  INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    label      TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'queued',
    detail     TEXT NOT NULL DEFAULT '',
    done       INTEGER NOT NULL DEFAULT 0,
    total      INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
"""

# Columns added after the first release. Applied to existing databases on open
# so upgrading never means losing annotations.
MIGRATIONS = {
    "ocr_text": "TEXT NOT NULL DEFAULT ''",
    "description": "TEXT NOT NULL DEFAULT ''",
    "source_url": "TEXT NOT NULL DEFAULT ''",
    "ocr_at": "REAL NOT NULL DEFAULT 0",
    "enriched_at": "REAL NOT NULL DEFAULT 0",
    # Duplicate detection: exact bytes (sha256), and space-joined perceptual
    # hashes of several frames (phash) so re-encodes at different lengths match.
    "sha256": "TEXT NOT NULL DEFAULT ''",
    "phash": "TEXT NOT NULL DEFAULT ''",
    "favorite": "INTEGER NOT NULL DEFAULT 0",
}


# Trashed files are named "<stamp>-<original>.gif", or "<stamp>-<n>-<original>"
# when the same name is deleted twice in one second. Parsing it back is what
# lets the trash list show real names and restore under them.
TRASH_NAME = re.compile(r"^(?P<stamp>\d+)-(?:(?P<seq>\d+)-)?(?P<original>.+\.gif)$", re.IGNORECASE)


@dataclass(frozen=True)
class Gif:
    id: int
    filename: str
    title: str
    tags: list[str]
    width: int
    height: int
    bytes: int
    added_at: float
    copies: int
    ocr_text: str = ""
    description: str = ""
    source_url: str = ""
    ocr_at: float = 0.0
    enriched_at: float = 0.0
    sha256: str = ""
    phash: str = ""
    favorite: int = 0

    def as_dict(self) -> dict:
        return {**self.__dict__, "url": f"/gifs/{self.filename}"}


def gif_dimensions(data: bytes) -> tuple[int, int]:
    """Read the logical screen size from a GIF header (bytes 6-9, little endian)."""
    if len(data) < 10 or not data.startswith((b"GIF87a", b"GIF89a")):
        return (0, 0)
    w = int.from_bytes(data[6:8], "little")
    h = int.from_bytes(data[8:10], "little")
    if w * h > 25_000_000 or w > 10000 or h > 10000:
        raise ValueError(f"GIF dimensions ({w}x{h}) exceed safe limits")
    return (w, h)


def safe_filename(name: str) -> str:
    """Slug a user-supplied name down to something safe to sit in the gifs dir."""
    stem = Path(name).stem
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-._")
    return f"{stem or 'gif'}.gif"


# Search words that ask a question about a GIF rather than matching its text.
# Borrowed from gifdex, which has "untagged"; the filing-oriented counterpart
# is worth having too. A GIF literally tagged "untagged" is not findable by
# that word any more, which is a fair trade for the shortcut.
FILTERS = {
    "untagged": lambda gif: not gif.tags,
    "undescribed": lambda gif: not gif.description.strip(),
    "untitled": lambda gif: not gif.title.strip(),
    # Never once copied: the prune shortlist. Search "unused" to review them.
    "unused": lambda gif: gif.copies == 0,
    "favorite": lambda gif: bool(gif.favorite),
    "favorites": lambda gif: bool(gif.favorite),
    "starred": lambda gif: bool(gif.favorite),
}


def split_tags(raw: str) -> list[str]:
    return [t for t in (part.strip().lower() for part in raw.replace(",", " ").split()) if t]


def looks_like_library(path: Path) -> bool:
    """A directory gifhole has used before, as opposed to any old folder."""
    return (path / "gifhole.db").is_file() or (path / "gifs").is_dir()


def move_library(source: Path, destination: Path) -> Path:
    """Move a whole library to a new location.

    Only the files move. Nothing in the database is rewritten because nothing
    in it is a path: rows store a bare filename and the root is supplied at
    runtime. That is a property worth keeping, since it makes relocating the
    library a plain directory move rather than a migration.
    """
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()

    if not source.is_dir():
        raise ValueError(f"no library at {source}")
    if not looks_like_library(source):
        raise ValueError(f"{source} does not look like a gifhole library")
    if destination == source:
        raise ValueError("the destination is where the library already is")
    # Moving a directory into its own subtree would recurse into the thing
    # being moved; shutil raises for this but late and less clearly.
    if source in destination.parents:
        raise ValueError("the destination is inside the library being moved")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError(f"{destination} is a file")
        if any(destination.iterdir()):
            raise ValueError(f"{destination} already exists and is not empty")
        # An empty directory is fine, but shutil.move would nest inside it, so
        # it goes and gets recreated by the move itself.
        destination.rmdir()

    destination.parent.mkdir(parents=True, exist_ok=True)
    # shutil.move, not rename: a library is very often being moved onto another
    # disk, and rename cannot cross filesystems.
    shutil.move(str(source), str(destination))
    return destination


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.gifs_dir = self.root / "gifs"
        self.trash_dir = self.root / ".trash"
        self.gifs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock:
            self.db = sqlite3.connect(
                self.root / "gifhole.db", check_same_thread=False, timeout=30.0
            )
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA busy_timeout = 5000")
            self.db.executescript(SCHEMA)
            self._migrate()
            self.db.commit()
        # Cache for the ambient "possible dupes" count. The scan is O(n^2) and
        # too slow for the request thread, so it runs in a background job and
        # the result is cached. Invalidation is keyed on `library_signature()`,
        # a cheap fingerprint of the data, NOT on remembering to flag every
        # mutation: the classic way to get cache bugs is scattered manual
        # invalidation, so the cache is stale exactly when the data changed.
        self._dup_count: int | None = None
        self._dup_signature: tuple | None = None

    def _migrate(self) -> None:
        with self._lock:
            existing = {row["name"] for row in self.db.execute("PRAGMA table_info(gifs)")}
            for column, spec in MIGRATIONS.items():
                if column not in existing:
                    self.db.execute(f"ALTER TABLE gifs ADD COLUMN {column} {spec}")  # noqa: S608
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS dismissed_duplicates ("
                "gif1_id INTEGER NOT NULL, "
                "gif2_id INTEGER NOT NULL, "
                "created_at REAL NOT NULL, "
                "PRIMARY KEY (gif1_id, gif2_id))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS confuser_hashes ("
                "phash_val TEXT PRIMARY KEY, "
                "hit_count INTEGER NOT NULL DEFAULT 1, "
                "updated_at REAL NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS job_queue ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "kind TEXT NOT NULL, "
                "label TEXT NOT NULL, "
                "payload TEXT NOT NULL DEFAULT '{}', "
                "status TEXT NOT NULL DEFAULT 'queued', "
                "detail TEXT NOT NULL DEFAULT '', "
                "done INTEGER NOT NULL DEFAULT 0, "
                "total INTEGER NOT NULL DEFAULT 0, "
                "created_at REAL NOT NULL)"
            )

    # -- reads ---------------------------------------------------------------

    def _row_to_gif(self, row: sqlite3.Row) -> Gif:
        return Gif(
            id=row["id"],
            filename=row["filename"],
            title=row["title"],
            tags=split_tags(row["tags"]),
            width=row["width"],
            height=row["height"],
            bytes=row["bytes"],
            added_at=row["added_at"],
            copies=row["copies"],
            ocr_text=row["ocr_text"],
            description=row["description"],
            source_url=row["source_url"],
            ocr_at=row["ocr_at"],
            enriched_at=row["enriched_at"],
            sha256=row["sha256"],
            phash=row["phash"],
            favorite=row["favorite"],
        )

    def list_gifs(self, query: str = "", sort: str = "added") -> list[Gif]:
        order = {
            "added": "added_at DESC",
            "name": "COALESCE(NULLIF(title, ''), filename) COLLATE NOCASE ASC",
            "copies": "copies DESC, added_at DESC",
            "favorite": "favorite DESC, added_at DESC",
        }.get(sort, "added_at DESC")
        with self._lock:
            rows = self.db.execute(f"SELECT * FROM gifs ORDER BY {order}").fetchall()  # noqa: S608
            gifs = [self._row_to_gif(r) for r in rows]
        terms = split_tags(query)
        if not terms:
            return gifs
        # Split the query into questions and plain text, so "untagged cat"
        # means "has no tags, and mentions cat somewhere".
        checks = [FILTERS[t] for t in terms if t in FILTERS]
        words = [t for t in terms if t not in FILTERS]
        return [g for g in gifs if all(c(g) for c in checks) and _matches(g, words)]

    def get(self, gif_id: int) -> Gif | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM gifs WHERE id = ?", (gif_id,)).fetchone()
            return self._row_to_gif(row) if row else None

    def all_tags(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        with self._lock:
            rows = self.db.execute("SELECT tags FROM gifs").fetchall()
        for row in rows:
            for tag in split_tags(row["tags"]):
                counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    # -- writes --------------------------------------------------------------

    def add_bytes(self, name: str, data: bytes, tags: str = "", source_url: str = "") -> Gif:
        """Write a GIF into the library, uniquifying the filename on collision."""
        if not data.startswith((b"GIF87a", b"GIF89a")):
            raise ValueError("not a GIF file")
        filename = safe_filename(name)
        path = self.gifs_dir / filename
        stem = path.stem
        n = 2
        while path.exists():
            filename = f"{stem}-{n}.gif"
            path = self.gifs_dir / filename
            n += 1
        path.write_bytes(data)
        return self._index(path, tags=tags, source_url=source_url, data=data)

    def _index(
        self, path: Path, tags: str = "", source_url: str = "", data: bytes | None = None
    ) -> Gif:
        if data is None:
            # Only the header is needed for dimensions, and stat() gives the
            # size. Reading whole files here made rescan read every byte in the
            # library to extract ten bytes per file.
            with path.open("rb") as handle:
                header = handle.read(10)
            width, height = gif_dimensions(header)
            size = path.stat().st_size
        else:
            width, height = gif_dimensions(data)
            size = len(data)
        sha = dedupe.content_hash(data if data is not None else path.read_bytes())
        phash = dedupe.perceptual_hash(path)
        with self._lock:
            cur = self.db.execute(
                """INSERT INTO gifs
                       (filename, title, tags, width, height, bytes, added_at, source_url,
                        sha256, phash)
                   VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(filename) DO UPDATE SET width=excluded.width,
                       height=excluded.height, bytes=excluded.bytes,
                       sha256=excluded.sha256, phash=excluded.phash""",
                (path.name, tags, width, height, size, time.time(), source_url, sha, phash),
            )
            self.db.commit()
            if cur.lastrowid:
                gif_id = cur.lastrowid
            else:
                row = self.db.execute(
                    "SELECT id FROM gifs WHERE filename = ?", (path.name,)
                ).fetchone()
                gif_id = row["id"]
            gif = self.get(gif_id)
        assert gif is not None
        return gif

    def update(
        self,
        gif_id: int,
        *,
        title: str | None = None,
        tags: str | None = None,
        description: str | None = None,
        favorite: bool | int | None = None,
    ) -> Gif | None:
        with self._lock:
            if title is not None:
                self.db.execute("UPDATE gifs SET title = ? WHERE id = ?", (title.strip(), gif_id))
            if description is not None:
                # Editing by hand does not stamp enriched_at: that marks "Claude has
                # seen this", and a batch describe should still skip it afterwards
                # only if Claude actually did.
                self.db.execute(
                    "UPDATE gifs SET description = ? WHERE id = ?", (description.strip(), gif_id)
                )
            if tags is not None:
                self.db.execute(
                    "UPDATE gifs SET tags = ? WHERE id = ?", (" ".join(split_tags(tags)), gif_id)
                )
            if favorite is not None:
                self.db.execute(
                    "UPDATE gifs SET favorite = ? WHERE id = ?", (1 if favorite else 0, gif_id)
                )
            self.db.commit()
            return self.get(gif_id)

    def set_ocr(self, gif_id: int, text: str) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE gifs SET ocr_text = ?, ocr_at = ? WHERE id = ?",
                (text, time.time(), gif_id),
            )
            self.db.commit()

    def set_enrichment(self, gif_id: int, description: str, tags: str = "") -> None:
        """Store a Claude description and tags: the description replaces what
        was there, the tags merge into it. The two are treated differently on
        purpose. A GIF has one description, so a re-describe should overwrite a
        bad earlier one rather than leave it. Tags are a set you also curate by
        hand, so describe adds to them and never removes, matching the
        bulk-tag rule. The UI still snapshots both first and offers a one-step
        undo, so a describe you did not want can be taken back whole."""
        with self._lock:
            gif = self.get(gif_id)
            if gif is None:
                return
            merged = list(dict.fromkeys(gif.tags + split_tags(tags)))
            self.db.execute(
                "UPDATE gifs SET description = ?, tags = ?, enriched_at = ? WHERE id = ?",
                (description.strip(), " ".join(merged), time.time(), gif_id),
            )
            self.db.commit()

    def get_dismissed_duplicate_pairs(self) -> set[tuple[int, int]]:
        with self._lock:
            rows = self.db.execute("SELECT gif1_id, gif2_id FROM dismissed_duplicates").fetchall()
            return {(r["gif1_id"], r["gif2_id"]) for r in rows}

    def get_confuser_hashes(self, min_hits: int = 1) -> set[int]:
        with self._lock:
            rows = self.db.execute(
                "SELECT phash_val FROM confuser_hashes WHERE hit_count >= ?", (min_hits,)
            ).fetchall()
            return {int(r["phash_val"], 16) for r in rows if r["phash_val"]}

    def dismiss_duplicates(self, gif_ids: list[int]) -> int:
        """Mark a set of GIF ids as not duplicates of one another.

        Feeds back matching frame hashes into confuser_hashes to prevent
        similar false matches across other GIFs in the library.
        """
        if len(gif_ids) < 2:
            return 0
        gifs = [self.get(gid) for gid in gif_ids]
        valid_gifs = [g for g in gifs if g is not None]
        now = time.time()
        dismissed_count = 0

        with self._lock:
            for i, ga in enumerate(valid_gifs):
                for gb in valid_gifs[i + 1 :]:
                    low_id, high_id = min(ga.id, gb.id), max(ga.id, gb.id)
                    self.db.execute(
                        "INSERT OR IGNORE INTO dismissed_duplicates (gif1_id, gif2_id, created_at) "
                        "VALUES (?, ?, ?)",
                        (low_id, high_id, now),
                    )
                    dismissed_count += 1

                    # Feedback loop: find which frames matched and record as confusers
                    if ga.phash and gb.phash:
                        matched = dedupe.matching_frame_hashes(ga.phash, gb.phash)
                        for h_val in matched:
                            self.db.execute(
                                "INSERT INTO confuser_hashes (phash_val, hit_count, updated_at) "
                                "VALUES (?, 1, ?) "
                                "ON CONFLICT(phash_val) DO UPDATE SET "
                                "hit_count = hit_count + 1, updated_at = excluded.updated_at",
                                (h_val, now),
                            )
            self.db.commit()
        return dismissed_count

    def find_duplicates(
        self, data: bytes, path: Path | None = None, candidate_id: int | None = None
    ) -> list[tuple[Gif, str]]:
        """What already in the library looks like this GIF.

        Returns (gif, "exact" | "near"), exact first. Nothing is decided here:
        the caller shows these to the user, because only they can say whether a
        near match is the same GIF or a different cut of the same scene.
        """
        sha = dedupe.content_hash(data)
        phash = dedupe.perceptual_hash(path) if path else ""
        exact, near = [], []
        dismissed = self.get_dismissed_duplicate_pairs()
        confusers = self.get_confuser_hashes()
        for gif in self.list_gifs():
            if candidate_id is not None:
                pair = (min(candidate_id, gif.id), max(candidate_id, gif.id))
                if pair in dismissed:
                    continue
            if gif.sha256 and gif.sha256 == sha:
                exact.append((gif, "exact"))
            elif phash and dedupe.is_near(gif.phash, phash, confuser_hashes=confusers):
                near.append((gif, "near"))
        return exact + near

    def backfill_hashes(self, limit: int | None = None) -> int:
        """Hash rows added before deduping existed, so they can be matched too.

        Without this a library built up over months would only ever detect
        duplicates of things added after the upgrade.
        """
        # Also re-hash old single-frame phashes (one value, no space): the hash
        # became multi-frame, and a library built under the old scheme would
        # otherwise keep missing re-encodes the new one catches. instr(...)=0
        # matches both empty and single-hash; a legit one-frame GIF gets
        # re-hashed on each run, which is cheap and rare.
        with self._lock:
            sql = (
                "SELECT id, filename FROM gifs "
                "WHERE sha256 = '' OR phash = '' OR instr(phash, ' ') = 0"
            )
            rows = self.db.execute(sql).fetchall()
        done = 0
        for row in rows[:limit] if limit else rows:
            path = self.gifs_dir / row["filename"]
            if not path.is_file():
                continue
            try:
                sha = dedupe.content_hash(path.read_bytes())
            except OSError:
                continue
            with self._lock:
                self.db.execute(
                    "UPDATE gifs SET sha256 = ?, phash = ? WHERE id = ?",
                    (sha, dedupe.perceptual_hash(path), row["id"]),
                )
                self.db.commit()
            done += 1
        return done

    def duplicate_groups(self) -> list[list[Gif]]:
        """Duplicates already sitting in the library, grouped."""
        gifs = [g for g in self.list_gifs() if g.sha256 or g.phash]
        frames = {g.id: dedupe.frame_ints(g.phash) for g in gifs}
        dismissed = self.get_dismissed_duplicate_pairs()
        confusers = self.get_confuser_hashes()
        seen: set[int] = set()
        groups = []
        for i, gif in enumerate(gifs):
            if gif.id in seen:
                continue
            group = [gif]
            for other in gifs[i + 1 :]:
                if other.id in seen:
                    continue
                pair = (min(gif.id, other.id), max(gif.id, other.id))
                if pair in dismissed:
                    continue
                same = (gif.sha256 and gif.sha256 == other.sha256) or dedupe.frames_near(
                    frames[gif.id], frames[other.id], confuser_hashes=confusers
                )
                if same:
                    group.append(other)
                    seen.add(other.id)
            if len(group) > 1:
                seen.add(gif.id)
                groups.append(group)
        return groups

    def library_signature(self) -> tuple:
        """A cheap fingerprint of the library, over one indexed aggregate, that
        changes on any add, remove, or re-hash. The duplicate-count cache is
        keyed on this, so it is stale exactly when the data it summarises is,
        with no per-mutation bookkeeping to forget."""
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(id), 0), COALESCE(SUM(LENGTH(phash)), 0) FROM gifs"
            ).fetchone()
            dismissed = self.db.execute("SELECT COUNT(*) FROM dismissed_duplicates").fetchone()[0]
            return (*tuple(row), dismissed)

    # What a library-wide job should touch. Named rather than boolean flags so
    # the UI can show a count for exactly what it is about to spend money on.
    SCOPES = ("missing_description", "missing_tags", "missing_either", "all")

    def in_scope(self, scope: str) -> list[Gif]:
        gifs = self.list_gifs()
        if scope == "all":
            return gifs
        if scope == "missing_description":
            return [g for g in gifs if not g.description.strip()]
        if scope == "missing_tags":
            return [g for g in gifs if not g.tags]
        if scope == "missing_either":
            return [g for g in gifs if not g.description.strip() or not g.tags]
        raise ValueError(f"unknown scope: {scope}")

    def stats(self) -> dict:
        """Counts behind the library panel: what each describe scope covers, and
        library-health numbers pointed at future use (what to prune, filing
        debt, which tags are load-bearing)."""
        gifs = self.list_gifs()
        return {
            "total": len(gifs),
            "missing_description": sum(1 for g in gifs if not g.description.strip()),
            "missing_tags": sum(1 for g in gifs if not g.tags),
            "missing_either": sum(1 for g in gifs if not g.description.strip() or not g.tags),
            "all": len(gifs),
            "never_ocr": sum(1 for g in gifs if not g.ocr_at),
            "described": sum(1 for g in gifs if g.enriched_at),
            "tags": len(self.all_tags()),
            "bytes": sum(g.bytes for g in gifs),
            # Pruning-oriented, actionable via the matching filter word.
            "unused": sum(1 for g in gifs if g.copies == 0),
            "untitled": sum(1 for g in gifs if not g.title.strip()),
            # Vocabulary at a glance: the heaviest tags, for spotting the ones
            # worth merging or the shelves that hold most of the library.
            "top_tags": [{"tag": t, "count": c} for t, c in self.all_tags()[:12]],
        }

    def retag(
        self, ids: list[int], add: list[str] | tuple[str, ...] = (), remove: list[str] = ()
    ) -> list[int]:
        """Add and remove tags across many GIFs in one pass.

        Adding is a union, not a replace: filing a batch under "reaction" must
        not wipe whatever each one was already tagged with. Returns the ids
        that actually changed, so an unchanged GIF costs no write.
        """
        drop = set(remove)
        wanted = list(dict.fromkeys(add))
        changed = []
        with self._lock:
            for gif_id in ids:
                gif = self.get(gif_id)
                if gif is None:
                    continue
                tags = [t for t in gif.tags if t not in drop]
                tags += [t for t in wanted if t not in tags]
                if tags == gif.tags:
                    continue
                self.db.execute("UPDATE gifs SET tags = ? WHERE id = ?", (" ".join(tags), gif_id))
                changed.append(gif_id)
            self.db.commit()
        return changed

    def needing_ocr(self) -> list[Gif]:
        return [g for g in self.list_gifs() if not g.ocr_at]

    def bump_copies(self, gif_id: int) -> None:
        with self._lock:
            self.db.execute("UPDATE gifs SET copies = copies + 1 WHERE id = ?", (gif_id,))
            self.db.commit()

    def remove(self, gif_id: int) -> str | None:
        """Move a GIF to .trash rather than deleting it, then drop its row.

        Returns the name it was given in .trash, which is what makes the
        removal undoable; None if there was no such GIF.
        """
        with self._lock:
            gif = self.get(gif_id)
            if gif is None:
                return None
            trashed = None
            src = self.gifs_dir / gif.filename
            if src.exists():
                self.trash_dir.mkdir(parents=True, exist_ok=True)
                # rename() replaces silently, so deleting the same filename twice
                # inside one second would destroy the first trashed copy. Nothing
                # in .trash may ever be overwritten.
                stamp = int(time.time())
                dest = self.trash_dir / f"{stamp}-{gif.filename}"
                n = 2
                while dest.exists():
                    dest = self.trash_dir / f"{stamp}-{n}-{gif.filename}"
                    n += 1
                src.rename(dest)
                trashed = dest.name
            self.db.execute("DELETE FROM gifs WHERE id = ?", (gif_id,))
            self.db.commit()
            return trashed or ""

    # -- the trash -----------------------------------------------------------

    def _trash_path(self, name: str) -> Path:
        """Resolve a trash entry by name, refusing anything outside .trash.

        The name arrives from the client, so `../../etc/passwd` has to bounce
        here rather than at the caller.
        """
        path = (self.trash_dir / name).resolve()
        if path.parent != self.trash_dir.resolve() or not path.is_file():
            raise FileNotFoundError(name)
        return path

    def trash_entries(self) -> list[dict]:
        """What is in .trash, newest first, with the original name recovered."""
        if not self.trash_dir.is_dir():
            return []
        entries = []
        for path in self.trash_dir.iterdir():
            if not path.is_file() or path.suffix.lower() != ".gif":
                continue
            match = TRASH_NAME.match(path.name)
            stat = path.stat()
            entries.append(
                {
                    "name": path.name,
                    "filename": match.group("original") if match else path.name,
                    "bytes": stat.st_size,
                    # Prefer the stamp in the name: it records when the delete
                    # happened, where mtime only records the last write.
                    "deleted_at": float(match.group("stamp")) if match else stat.st_mtime,
                }
            )
        return sorted(entries, key=lambda e: e["deleted_at"], reverse=True)

    def restore(self, name: str) -> Gif:
        """Put a trashed GIF back, under its original name where that is free."""
        with self._lock:
            path = self._trash_path(name)
            match = TRASH_NAME.match(path.name)
            original = match.group("original") if match else path.name
            dest = self.gifs_dir / original
            stem = dest.stem
            n = 2
            while dest.exists():
                dest = self.gifs_dir / f"{stem}-{n}.gif"
                n += 1
            path.rename(dest)
            return self._index(dest)

    def purge(self, name: str) -> None:
        """Delete one trashed file for good. There is nothing after this."""
        self._trash_path(name).unlink()

    def empty_trash(self) -> int:
        count = 0
        for entry in self.trash_entries():
            self.purge(entry["name"])
            count += 1
        return count

    def clear_library(self) -> list[str]:
        """Move every GIF to .trash. Recoverable, unlike emptying the trash."""
        return [name for g in self.list_gifs() if (name := self.remove(g.id)) is not None]

    def rescan(self) -> dict[str, int]:
        """Index new files on disk and forget rows whose file is gone."""
        # Case-insensitive: a hand-dropped FOO.GIF is still a GIF, and the
        # folder is the source of truth. glob("*.gif") would ignore it forever.
        on_disk = {
            p.name for p in self.gifs_dir.iterdir() if p.is_file() and p.suffix.lower() == ".gif"
        }
        with self._lock:
            known = {r["filename"] for r in self.db.execute("SELECT filename FROM gifs")}
            for name in sorted(on_disk - known):
                self._index(self.gifs_dir / name)
            for name in known - on_disk:
                self.db.execute("DELETE FROM gifs WHERE filename = ?", (name,))
            self.db.commit()
        return {"added": len(on_disk - known), "removed": len(known - on_disk)}

    def recompute_duplicate_count(self) -> int:
        """Refresh the cached possible-duplicates count and stamp the signature
        it was computed at. Runs in a background job (the scan is O(n^2)).
        Signature captured before the scan, so a change during it just triggers
        one more recompute rather than marking a stale count fresh."""
        sig = self.library_signature()
        count = len(self.duplicate_groups())
        with self._lock:
            self._dup_count = count
            self._dup_signature = sig
        return self._dup_count


def _matches(gif: Gif, terms: list[str]) -> bool:
    # OCR text earns its keep here: searching "nope" finds the GIF with NOPE
    # burned into it, without anyone having tagged it.
    haystack = " ".join(
        [gif.filename, gif.title, " ".join(gif.tags), gif.ocr_text, gif.description]
    ).lower()
    return all(term in haystack for term in terms)

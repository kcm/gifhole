"""SQLite-backed metadata for the GIF library.

The files on disk are the source of truth: the database only annotates them
(title, tags, copy counts). A rescan reconciles the two, so deleting the
database loses annotations but never GIFs.
"""

from __future__ import annotations

import re
import sqlite3
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
    copies    INTEGER NOT NULL DEFAULT 0
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
    # Duplicate detection: exact bytes, and one perceptual hash of a frame.
    "sha256": "TEXT NOT NULL DEFAULT ''",
    "phash": "TEXT NOT NULL DEFAULT ''",
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

    def as_dict(self) -> dict:
        return {**self.__dict__, "url": f"/gifs/{self.filename}"}


def gif_dimensions(data: bytes) -> tuple[int, int]:
    """Read the logical screen size from a GIF header (bytes 6-9, little endian)."""
    if len(data) < 10 or not data.startswith((b"GIF87a", b"GIF89a")):
        return (0, 0)
    return (
        int.from_bytes(data[6:8], "little"),
        int.from_bytes(data[8:10], "little"),
    )


def safe_filename(name: str) -> str:
    """Slug a user-supplied name down to something safe to sit in the gifs dir."""
    stem = Path(name).stem
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-._")
    return f"{stem or 'gif'}.gif"


def split_tags(raw: str) -> list[str]:
    return [t for t in (part.strip().lower() for part in raw.replace(",", " ").split()) if t]


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.gifs_dir = self.root / "gifs"
        self.trash_dir = self.root / ".trash"
        self.gifs_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "gifhole.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        existing = {row["name"] for row in self.db.execute("PRAGMA table_info(gifs)")}
        for column, spec in MIGRATIONS.items():
            if column not in existing:
                self.db.execute(f"ALTER TABLE gifs ADD COLUMN {column} {spec}")  # noqa: S608

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
        )

    def list_gifs(self, query: str = "", sort: str = "added") -> list[Gif]:
        order = {
            "added": "added_at DESC",
            "name": "COALESCE(NULLIF(title, ''), filename) COLLATE NOCASE ASC",
            "copies": "copies DESC, added_at DESC",
        }.get(sort, "added_at DESC")
        rows = self.db.execute(f"SELECT * FROM gifs ORDER BY {order}").fetchall()  # noqa: S608
        gifs = [self._row_to_gif(r) for r in rows]
        terms = split_tags(query)
        if not terms:
            return gifs
        return [g for g in gifs if _matches(g, terms)]

    def get(self, gif_id: int) -> Gif | None:
        row = self.db.execute("SELECT * FROM gifs WHERE id = ?", (gif_id,)).fetchone()
        return self._row_to_gif(row) if row else None

    def all_tags(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for row in self.db.execute("SELECT tags FROM gifs"):
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
        gif_id = (
            cur.lastrowid
            or self.db.execute("SELECT id FROM gifs WHERE filename = ?", (path.name,)).fetchone()[
                "id"
            ]
        )
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
    ) -> Gif | None:
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
        self.db.commit()
        return self.get(gif_id)

    def set_ocr(self, gif_id: int, text: str) -> None:
        self.db.execute(
            "UPDATE gifs SET ocr_text = ?, ocr_at = ? WHERE id = ?",
            (text, time.time(), gif_id),
        )
        self.db.commit()

    def set_enrichment(self, gif_id: int, description: str, tags: str = "") -> None:
        """Store a Claude-generated description, merging any suggested tags."""
        gif = self.get(gif_id)
        if gif is None:
            return
        merged = list(dict.fromkeys(gif.tags + split_tags(tags)))
        self.db.execute(
            "UPDATE gifs SET description = ?, tags = ?, enriched_at = ? WHERE id = ?",
            (description.strip(), " ".join(merged), time.time(), gif_id),
        )
        self.db.commit()

    def find_duplicates(self, data: bytes, path: Path | None = None) -> list[tuple[Gif, str]]:
        """What already in the library looks like this GIF.

        Returns (gif, "exact" | "near"), exact first. Nothing is decided here:
        the caller shows these to the user, because only they can say whether a
        near match is the same GIF or a different cut of the same scene.
        """
        sha = dedupe.content_hash(data)
        phash = dedupe.perceptual_hash(path) if path else ""
        exact, near = [], []
        for gif in self.list_gifs():
            if gif.sha256 and gif.sha256 == sha:
                exact.append((gif, "exact"))
            elif phash and dedupe.is_near(gif.phash, phash):
                near.append((gif, "near"))
        return exact + near

    def backfill_hashes(self, limit: int | None = None) -> int:
        """Hash rows added before deduping existed, so they can be matched too.

        Without this a library built up over months would only ever detect
        duplicates of things added after the upgrade.
        """
        rows = self.db.execute(
            "SELECT id, filename FROM gifs WHERE sha256 = '' OR phash = ''"
        ).fetchall()
        done = 0
        for row in rows[:limit] if limit else rows:
            path = self.gifs_dir / row["filename"]
            if not path.is_file():
                continue
            try:
                sha = dedupe.content_hash(path.read_bytes())
            except OSError:
                continue
            self.db.execute(
                "UPDATE gifs SET sha256 = ?, phash = ? WHERE id = ?",
                (sha, dedupe.perceptual_hash(path), row["id"]),
            )
            done += 1
        self.db.commit()
        return done

    def duplicate_groups(self) -> list[list[Gif]]:
        """Duplicates already sitting in the library, grouped."""
        gifs = [g for g in self.list_gifs() if g.sha256 or g.phash]
        seen: set[int] = set()
        groups = []
        for i, gif in enumerate(gifs):
            if gif.id in seen:
                continue
            group = [gif]
            for other in gifs[i + 1 :]:
                if other.id in seen:
                    continue
                same = (gif.sha256 and gif.sha256 == other.sha256) or dedupe.is_near(
                    gif.phash, other.phash
                )
                if same:
                    group.append(other)
                    seen.add(other.id)
            if len(group) > 1:
                seen.add(gif.id)
                groups.append(group)
        return groups

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
        self.db.execute("UPDATE gifs SET copies = copies + 1 WHERE id = ?", (gif_id,))
        self.db.commit()

    def remove(self, gif_id: int) -> str | None:
        """Move a GIF to .trash rather than deleting it, then drop its row.

        Returns the name it was given in .trash, which is what makes the
        removal undoable; None if there was no such GIF.
        """
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
        known = {r["filename"] for r in self.db.execute("SELECT filename FROM gifs")}
        for name in sorted(on_disk - known):
            self._index(self.gifs_dir / name)
        for name in known - on_disk:
            self.db.execute("DELETE FROM gifs WHERE filename = ?", (name,))
        self.db.commit()
        return {"added": len(on_disk - known), "removed": len(known - on_disk)}


def _matches(gif: Gif, terms: list[str]) -> bool:
    # OCR text earns its keep here: searching "nope" finds the GIF with NOPE
    # burned into it, without anyone having tagged it.
    haystack = " ".join(
        [gif.filename, gif.title, " ".join(gif.tags), gif.ocr_text, gif.description]
    ).lower()
    return all(term in haystack for term in terms)

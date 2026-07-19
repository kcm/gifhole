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
}


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
        cur = self.db.execute(
            """INSERT INTO gifs
                   (filename, title, tags, width, height, bytes, added_at, source_url)
               VALUES (?, '', ?, ?, ?, ?, ?, ?)
               ON CONFLICT(filename) DO UPDATE SET width=excluded.width,
                   height=excluded.height, bytes=excluded.bytes""",
            (path.name, tags, width, height, size, time.time(), source_url),
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
        self, gif_id: int, *, title: str | None = None, tags: str | None = None
    ) -> Gif | None:
        if title is not None:
            self.db.execute("UPDATE gifs SET title = ? WHERE id = ?", (title.strip(), gif_id))
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

    def needing_ocr(self) -> list[Gif]:
        return [g for g in self.list_gifs() if not g.ocr_at]

    def bump_copies(self, gif_id: int) -> None:
        self.db.execute("UPDATE gifs SET copies = copies + 1 WHERE id = ?", (gif_id,))
        self.db.commit()

    def remove(self, gif_id: int) -> bool:
        """Move a GIF to .trash rather than deleting it, then drop its row."""
        gif = self.get(gif_id)
        if gif is None:
            return False
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
        self.db.execute("DELETE FROM gifs WHERE id = ?", (gif_id,))
        self.db.commit()
        return True

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

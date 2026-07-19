import pytest

from gifhole.store import gif_dimensions, safe_filename, split_tags
from tests.conftest import make_gif, make_textured_gif


def test_dimensions_read_from_header():
    assert gif_dimensions(make_gif(320, 240)) == (320, 240)


def test_dimensions_of_non_gif_are_zero():
    assert gif_dimensions(b"not a gif at all") == (0, 0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd.gif"),  # directory parts are dropped entirely
        ("Happy Dance!.gif", "Happy-Dance.gif"),
        ("", "gif.gif"),
        ("...", "gif.gif"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_split_tags_normalizes():
    assert split_tags("Cats, DOGS  cats") == ["cats", "dogs", "cats"]


def test_add_rejects_non_gif(store):
    with pytest.raises(ValueError, match="not a GIF"):
        store.add_bytes("evil.gif", b"<html>nope</html>")


def test_add_uniquifies_colliding_names(store):
    a = store.add_bytes("dance.gif", make_gif())
    b = store.add_bytes("dance.gif", make_gif())
    assert (a.filename, b.filename) == ("dance.gif", "dance-2.gif")


def test_search_matches_name_and_tags(store):
    store.add_bytes("shrug.gif", make_gif(), tags="reaction meh")
    store.add_bytes("applause.gif", make_gif(), tags="reaction happy")
    assert len(store.list_gifs("reaction")) == 2
    assert [g.filename for g in store.list_gifs("shrug")] == ["shrug.gif"]
    assert [g.filename for g in store.list_gifs("reaction happy")] == ["applause.gif"]
    assert store.list_gifs("nonexistent") == []


def test_remove_moves_file_to_trash_not_oblivion(store):
    gif = store.add_bytes("bye.gif", make_gif())
    trashed = store.remove(gif.id)
    assert trashed and trashed.endswith("bye.gif")
    assert not (store.gifs_dir / "bye.gif").exists()
    assert len(list(store.trash_dir.glob("*bye.gif"))) == 1
    assert store.get(gif.id) is None


def test_remove_reports_no_such_gif_distinctly_from_a_missing_file(store):
    """None means "no such GIF"; "" means the row went but no file was there."""
    assert store.remove(9999) is None
    gif = store.add_bytes("ghost.gif", make_gif())
    (store.gifs_dir / "ghost.gif").unlink()
    assert store.remove(gif.id) == ""


def test_trash_listing_recovers_the_original_name(store):
    gif = store.add_bytes("hello.gif", make_gif())
    store.remove(gif.id)
    (entry,) = store.trash_entries()
    assert entry["filename"] == "hello.gif"
    assert entry["name"].endswith("-hello.gif")
    assert entry["bytes"] > 0


def test_restore_puts_it_back_under_its_original_name(store):
    gif = store.add_bytes("comeback.gif", make_gif())
    name = store.remove(gif.id)
    restored = store.restore(name)
    assert restored.filename == "comeback.gif"
    assert (store.gifs_dir / "comeback.gif").exists()
    assert store.trash_entries() == []
    assert [g.filename for g in store.list_gifs()] == ["comeback.gif"]


def test_restore_does_not_clobber_a_live_file_of_the_same_name(store):
    """The name was reused while the old one sat in the trash; keep both."""
    first = store.add_bytes("dupe.gif", make_gif())
    name = store.remove(first.id)
    store.add_bytes("dupe.gif", make_gif(16, 16))
    restored = store.restore(name)
    assert restored.filename == "dupe-2.gif"
    assert (store.gifs_dir / "dupe.gif").exists()
    assert len(store.list_gifs()) == 2


def test_purge_refuses_to_escape_the_trash_directory(store):
    """The name comes from the client, so traversal has to bounce here."""
    victim = store.gifs_dir / "keepme.gif"
    victim.write_bytes(make_gif())
    with pytest.raises(FileNotFoundError):
        store.purge("../gifs/keepme.gif")
    assert victim.exists()


def test_empty_trash_destroys_only_the_trash(store):
    for name in ("a.gif", "b.gif"):
        store.remove(store.add_bytes(name, make_gif()).id)
    kept = store.add_bytes("kept.gif", make_gif())
    assert store.empty_trash() == 2
    assert store.trash_entries() == []
    assert (store.gifs_dir / kept.filename).exists()


def test_clear_library_is_recoverable(store):
    for name in ("a.gif", "b.gif", "c.gif"):
        store.add_bytes(name, make_gif())
    trashed = store.clear_library()
    assert len(trashed) == 3
    assert store.list_gifs() == []
    # The point of clearing into the trash: every one of them can come back.
    for name in trashed:
        store.restore(name)
    assert len(store.list_gifs()) == 3


def test_rescan_picks_up_and_forgets_files(store):
    (store.gifs_dir / "manual.gif").write_bytes(make_gif())
    assert store.rescan() == {"added": 1, "removed": 0}
    assert [g.filename for g in store.list_gifs()] == ["manual.gif"]

    (store.gifs_dir / "manual.gif").unlink()
    assert store.rescan() == {"added": 0, "removed": 1}
    assert store.list_gifs() == []


def test_tag_counts(store):
    store.add_bytes("a.gif", make_gif(), tags="reaction")
    store.add_bytes("b.gif", make_gif(), tags="reaction cat")
    assert store.all_tags() == [("reaction", 2), ("cat", 1)]


def test_trash_never_overwrites_an_earlier_copy(store):
    """Deleting the same filename twice in one second must keep both."""
    for _ in range(3):
        gif = store.add_bytes("dupe.gif", make_gif())
        store.remove(gif.id)
    assert len(list(store.trash_dir.iterdir())) == 3


def test_rescan_picks_up_uppercase_extensions(store):
    """The folder is the source of truth, so FOO.GIF counts as a GIF."""
    (store.gifs_dir / "SHOUTY.GIF").write_bytes(make_gif())
    assert store.rescan()["added"] == 1
    assert [g.filename for g in store.list_gifs()] == ["SHOUTY.GIF"]


# -- duplicate detection -----------------------------------------------------


def test_identical_bytes_are_an_exact_duplicate(store):
    data = make_textured_gif(1)
    store.add_bytes("first.gif", data)
    matches = store.find_duplicates(data)
    assert [kind for _, kind in matches] == ["exact"]
    assert matches[0][0].filename == "first.gif"


def test_a_visibly_different_gif_is_not_a_duplicate(store):
    store.add_bytes("first.gif", make_textured_gif(1))
    assert store.find_duplicates(make_textured_gif(2)) == []


def test_a_resized_copy_is_caught_as_a_near_duplicate(store):
    """The case sha256 cannot catch: same picture, different bytes."""
    store.add_bytes("original.gif", make_textured_gif(3, 64, 48))
    matches = store.find_duplicates(*_staged(make_textured_gif(3, 32, 24)))
    assert [kind for _, kind in matches] == ["near"]


def test_a_flat_image_gets_no_perceptual_hash(store):
    """Every solid-colour GIF dhashes alike, so they must not match on it."""
    gif = store.add_bytes("flat.gif", make_gif(32, 24))
    assert store.get(gif.id).phash == ""
    # A second, unrelated flat GIF must not be called a duplicate of the first.
    assert store.find_duplicates(make_gif(16, 16)) == []


def test_hashes_are_recorded_on_add(store):
    gif = store.add_bytes("hashed.gif", make_textured_gif(4))
    stored = store.get(gif.id)
    assert len(stored.sha256) == 64
    assert stored.phash


def test_backfill_hashes_covers_rows_added_before_deduping(store):
    gif = store.add_bytes("old.gif", make_textured_gif(7))
    store.db.execute("UPDATE gifs SET sha256 = '', phash = '' WHERE id = ?", (gif.id,))
    store.db.commit()
    assert store.backfill_hashes() == 1
    assert store.get(gif.id).sha256


def test_duplicate_groups_finds_copies_already_in_the_library(store):
    data = make_textured_gif(5)
    store.add_bytes("one.gif", data)
    store.add_bytes("two.gif", data)
    store.add_bytes("unrelated.gif", make_textured_gif(6))
    groups = store.duplicate_groups()
    assert len(groups) == 1
    assert sorted(g.filename for g in groups[0]) == ["one.gif", "two.gif"]


def _staged(data: bytes):
    """Write bytes somewhere readable, since a perceptual hash needs a file."""
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp()) / "probe.gif"
    path.write_bytes(data)
    return data, path


# -- moving the library ------------------------------------------------------


def test_move_relocates_everything_and_keeps_annotations(tmp_path):
    """A move is files only: nothing in the database is a path, so nothing in
    it needs rewriting. This is what keeps relocating cheap."""
    from gifhole.store import Store, move_library

    source = Store(tmp_path / "before")
    gif = source.add_bytes("keeper.gif", make_textured_gif(50), tags="cat reaction")
    source.update(gif.id, title="Keeper", description="the good one")
    source.remove(source.add_bytes("binned.gif", make_textured_gif(51)).id)
    source.db.close()

    destination = tmp_path / "moved" / "library"
    assert move_library(source.root, destination) == destination.resolve()
    assert not source.root.exists()

    reopened = Store(destination)
    (kept,) = reopened.list_gifs()
    assert kept.filename == "keeper.gif"
    assert kept.title == "Keeper"
    assert kept.tags == ["cat", "reaction"]
    assert kept.description == "the good one"
    # The trash comes along too, or "recoverable" would stop being true.
    assert len(reopened.trash_entries()) == 1


def test_move_into_an_empty_directory_does_not_nest(tmp_path):
    from gifhole.store import Store, move_library

    source = Store(tmp_path / "before")
    source.add_bytes("a.gif", make_textured_gif(52))
    source.db.close()
    destination = tmp_path / "empty"
    destination.mkdir()
    move_library(source.root, destination)
    assert (destination / "gifs" / "a.gif").is_file()
    assert not (destination / source.root.name).exists()


def test_move_refuses_a_non_empty_destination(tmp_path):
    """Merging into someone else's folder could silently mix two libraries."""
    from gifhole.store import Store, move_library

    source = Store(tmp_path / "before")
    source.add_bytes("a.gif", make_textured_gif(53))
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "something.txt").write_text("mine")
    with pytest.raises(ValueError, match="not empty"):
        move_library(source.root, occupied)
    assert source.root.exists()


def test_move_refuses_a_destination_inside_itself(store, tmp_path):
    from gifhole.store import move_library

    store.add_bytes("a.gif", make_textured_gif(54))
    with pytest.raises(ValueError, match="inside the library"):
        move_library(store.root, store.root / "nested")
    assert store.root.exists()


def test_move_refuses_a_directory_that_is_not_a_library(tmp_path):
    """Guards against a typo'd --root turning into a move of the wrong folder."""
    from gifhole.store import move_library

    stranger = tmp_path / "documents"
    (stranger / "sub").mkdir(parents=True)
    with pytest.raises(ValueError, match="does not look like"):
        move_library(stranger, tmp_path / "elsewhere")
    assert stranger.exists()


def test_move_refuses_to_move_onto_itself(store):
    from gifhole.store import move_library

    store.add_bytes("a.gif", make_textured_gif(55))
    with pytest.raises(ValueError, match="already is"):
        move_library(store.root, store.root)
    assert store.root.exists()

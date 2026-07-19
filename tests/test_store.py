import pytest

from gifhole.store import gif_dimensions, safe_filename, split_tags
from tests.conftest import make_gif


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

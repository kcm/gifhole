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
    assert store.remove(gif.id) is True
    assert not (store.gifs_dir / "bye.gif").exists()
    assert len(list(store.trash_dir.glob("*bye.gif"))) == 1
    assert store.get(gif.id) is None


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

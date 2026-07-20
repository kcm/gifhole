"""The process-console feed: a cursor-based tail of what the server is doing."""

from __future__ import annotations

from gifhole.logbus import LogBus


def test_since_zero_returns_everything_buffered():
    bus = LogBus()
    bus.emit("import", "one")
    bus.emit("ocr", "two")
    events, cursor = bus.since(0)
    assert [e["message"] for e in events] == ["one", "two"]
    assert cursor == 2


def test_a_cursor_returns_only_what_is_newer():
    """This is what stops the console replaying the whole buffer every poll."""
    bus = LogBus()
    bus.emit("import", "one")
    _, cursor = bus.since(0)
    bus.emit("ocr", "two")
    events, new_cursor = bus.since(cursor)
    assert [e["message"] for e in events] == ["two"]
    assert new_cursor == 2


def test_an_idle_poll_is_a_no_op_not_a_reset():
    """A cursor with nothing new must come back empty and keep the cursor, not
    fall back to the start of the buffer and replay it."""
    bus = LogBus()
    bus.emit("import", "one")
    _, cursor = bus.since(0)
    events, again = bus.since(cursor)
    assert events == []
    assert again == cursor


def test_the_ring_buffer_drops_the_oldest():
    """Bounded on purpose: a long session must not grow without end. The cursor
    keeps counting up even as old events fall off the back."""
    bus = LogBus(keep=3)
    for i in range(5):
        bus.emit("import", str(i))
    events, cursor = bus.since(0)
    assert [e["message"] for e in events] == ["2", "3", "4"]
    assert cursor == 5


def test_the_endpoint_tails_by_cursor(tmp_path):
    from fastapi.testclient import TestClient

    from gifhole.app import create_app

    app = create_app(tmp_path, auto_ocr=False)
    app.state.bus.emit("import", "importing 2 selected")
    app.state.bus.emit("import", "added a.gif")
    client = TestClient(app)

    first = client.get("/api/log").json()
    assert [e["message"] for e in first["events"]] == ["importing 2 selected", "added a.gif"]

    app.state.bus.emit("ocr", "reading text: a.gif")
    tail = client.get("/api/log", params={"since": first["cursor"]}).json()
    assert [e["message"] for e in tail["events"]] == ["reading text: a.gif"]

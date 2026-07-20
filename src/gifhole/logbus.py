"""A small in-memory feed of what the server is doing, for the UI console.

Job status answers "is it working?". This answers "what is it doing right
now?": the sub-steps inside a job that never reach a status field, like
fetching one URL of a thread, calling the model, or which tags a description
added. The `` ` `` / `~` console is the only consumer.

Deliberately not a logging handler and not persisted. It is a ring buffer read
over HTTP by a cursor, so a client that was not watching simply misses the
lines it was not there for, exactly like a terminal. Kept general on purpose:
any code path can `emit()` a source and a message, so this is reusable beyond
import (a future long operation gets a console for free).
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    seq: int
    t: float
    source: str
    message: str
    level: str  # info | warn | error

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "t": self.t,
            "source": self.source,
            "message": self.message,
            "level": self.level,
        }


class LogBus:
    """Thread-safe ring buffer with a monotonic sequence.

    The worker thread emits and request threads drain, so every access takes
    the lock. `since()` returns only what is newer than a cursor, which is what
    lets the console poll cheaply and never replay a line twice.
    """

    def __init__(self, keep: int = 500) -> None:
        self._events: deque[Event] = deque(maxlen=keep)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def emit(self, source: str, message: str, level: str = "info") -> None:
        # No timestamp argument: the caller should not be able to lie about
        # when something happened, and the console orders by seq anyway.
        event = Event(next(self._seq), time.time(), source, message, level)
        with self._lock:
            self._events.append(event)

    def since(self, cursor: int) -> tuple[list[dict], int]:
        """Events newer than `cursor`, and the sequence to ask from next.

        A cursor of 0 asks for everything still buffered, which is how the
        console back-fills when it is first opened. The returned cursor is the
        newest seq seen, or the input when nothing is new, so an idle poll is a
        no-op rather than a reset to the start of the buffer.
        """
        with self._lock:
            fresh = [e.as_dict() for e in self._events if e.seq > cursor]
            last = self._events[-1].seq if self._events else cursor
        return fresh, max(cursor, last)

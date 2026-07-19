#!/usr/bin/env python3
"""Print one version's section from CHANGELOG.md.

Used by the release workflow so a GitHub release says the same thing as the
changelog, rather than being written twice and drifting. Runnable by hand:

    python scripts/release_notes.py 0.1.0
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# "## [0.1.0] - 2026-07-19", and the next heading of the same level ends it.
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]")


def notes_for(version: str, text: str) -> str:
    wanted = version.lstrip("v")
    lines = text.splitlines()
    out: list[str] = []
    collecting = False
    for line in lines:
        match = HEADING.match(line)
        if match:
            if collecting:
                break
            collecting = match.group("version").lstrip("v") == wanted
            continue
        if collecting:
            out.append(line)
    body = "\n".join(out).strip()
    if not body:
        raise SystemExit(f"no changelog section for {version}")
    return body


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: release_notes.py VERSION")
    print(notes_for(sys.argv[1], CHANGELOG.read_text()))


if __name__ == "__main__":
    main()

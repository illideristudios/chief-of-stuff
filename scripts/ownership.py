#!/usr/bin/env python3
"""The tracker's File ownership rows, read one way by every script that needs them.

A row names a context and the paths it owns, written as a table row `| context | paths |` or as a
bullet `- context: paths`. Audit read both and dispatch read only tables, so a row the audit
accepted was refused at launch (#74).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from md import cells as _cells, is_separator as _is_separator, section as _section

HEADING = "## File ownership"
_NONE = re.compile(r"none(?![\w./-])", re.I)


@dataclass(frozen=True)
class Row:
    context: str
    paths: str


def parse(text: str) -> list[Row]:
    """Every row in the section's body, in order; the table header and separator are not rows."""
    rows: list[Row] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")) and ":" in stripped:
            context, _, paths = stripped[2:].partition(":")
            rows.append(Row(context.strip(), paths.strip()))
            continue
        if not stripped.startswith("|"):
            continue
        cells = _cells(line)
        if _is_separator(cells) or len(cells) < 2 or cells[0].lower() == "context":
            continue
        rows.append(Row(cells[0], cells[1]))
    return rows


def cell(text: str, keys: set[str]) -> str | None:
    """The paths of the first File ownership row in the tracker `text` keyed on one of `keys`, or None."""
    return next((row.paths for row in parse("\n".join(_section(text, HEADING))) if row.context in keys), None)


def issue_scopes(body: str) -> list[tuple[str, str]] | None:
    """Issue `## File ownership`: `| Mode | Paths |` or `- write: paths` / `- read-only: paths`.

    Reuse the tracker row reader. Missing, empty or malformed declarations have no scope (None).
    Only an explicit `.` path declares the whole repository.
    Paths are literal repository-relative paths, just as for the tracker's overlap rule.
    """
    scopes: dict[str, str] = {}
    lines = _section(body, HEADING)
    for line in lines:
        if not line.strip():
            continue
        if line.strip().startswith("|"):
            cells = _cells(line)
            if len(cells) != 2:
                return None
            if _is_separator(cells) or [c.lower() for c in cells] in (["mode", "paths"], ["context", "paths"]):
                continue
        if not parse(line):
            return None
    for row in parse("\n".join(lines)):
        mode = row.context.strip().lower()
        if mode == "mode" and row.paths.lower() == "paths":
            continue
        if mode not in ("write", "read", "read-only"):
            return None
        if "`" in row.paths and not re.fullmatch(r"\s*`[^`]+`(?:\s*,\s*`[^`]+`)*\s*", row.paths):
            return None
        if any(token != token.strip() for token in re.findall(r"`([^`]+)`", row.paths)):
            return None
        paths = _paths(row.paths)
        if not paths:
            return None
        for path in paths:
            if (path.startswith("/") or (path != "." and any(part in ("", ".", "..") for part in path.split("/")))
                    or any(c in path for c in "*?[]`") or _paths(path) != [path]):
                return None
            # A duplicate write cannot be narrowed by a later read row.
            scopes[path] = "write" if mode == "write" or scopes.get(path) == "write" else "read"
    return list(scopes.items()) or None


def read_only(paths: str) -> bool:
    """A cell that starts with the word `none`, not the start of a path, owns nothing, so it changes nothing (#57)."""
    return bool(_NONE.match(paths.strip()))


def _paths(cell: str) -> list[str]:
    """The paths a cell owns: its backticked tokens, or its comma-separated words when it has none; the launcher's
    `; worktree ...` suffix is no part of it, and a read-only cell owns none. Segment-wise: no `./`, no trailing `/`; `.` is the whole repository."""
    cell = cell.split("; worktree", 1)[0]
    if read_only(cell):
        return []
    tokens = re.findall(r"`([^`]+)`", cell) or cell.split(",")
    return [p for p in (t.strip().rstrip("/").removeprefix("./") for t in tokens) if p]


def overlaps(a: str, b: str) -> bool:
    """One path of `a` equals, contains or is contained in one of `b` (`src/a/` and `src/a/x.py` do; `src/a` and `src/ab` do not); `.` contains every path.
    ponytail: a glob character is read as part of the name, so `src/*.py` overlaps only a path spelled that way; match globs when a tracker writes them."""
    return any("." in (x, y) or x == y or x.startswith(y + "/") or y.startswith(x + "/") for x in _paths(a) for y in _paths(b))

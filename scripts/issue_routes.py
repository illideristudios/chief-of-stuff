"""Where an issue's page lives, named by host, project and number (#412).

`/issues/<host>/<project path>/<number>` and `/source` after it, rendered into `issue-q-<sha256>[-source].html`. Two
issues numbered alike in different projects, or the same project path on two hosts, are two pages. A bare number names
no project: with a Backlog `/issues/<n>` is its issue, and without one it is a local namespace (an `IssueRef` with no
host, `issue-<n>.html`) that never matches a qualified reference. Pure naming: no filesystem, network or renderer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace

from backlog_ref import GITHUB, IssueRef, home_of, issue_ref

_KEY_GITLAB = re.compile(r"^([\w.-]+)/((?:[\w.-]+/)+[\w.-]+)#(\d+)$")  # the cache's `host/project#N`
_BARE = re.compile(r"#(\d+)")
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")  # no dot-led segment: `.` and `..` are never a project
_NUMBER = re.compile(r"0|[1-9][0-9]*")
_FILE = re.compile(r"issue-(\d+)(-source)?\.html")


def _ok(ref: IssueRef) -> bool:
    return not ref.host or all(_SEGMENT.fullmatch(s) for s in (ref.host, *ref.repo.split("/")))


def bare(number: int) -> IssueRef:
    return IssueRef("", number, "")


def resolve(cell: str, home) -> IssueRef | None:
    """The issue a tracker cell, Flow row key or board-source key names, or None for anything else. Never a trailing
    number: `Issue 8` is no issue."""
    text = cell.strip()
    ref = issue_ref(text, home, any_host=True)
    if ref is None:
        if m := _KEY_GITLAB.match(text):
            ref = IssueRef(m[2], int(m[3]), m[1])
        elif home is None and (m := _BARE.fullmatch(text)):
            ref = bare(int(m[1]))
    return ref if ref and _ok(ref) else None


def target(x: int | IssueRef, home) -> IssueRef:
    """`x` as an identity; a number is the Backlog's issue, or the local namespace's when there is no Backlog."""
    if isinstance(x, IssueRef):
        return x
    return IssueRef(home_of(home)[1], x, home_of(home)[0]) if home else bare(x)


def key(ref: IssueRef, home) -> str:
    """The key the board sources file the issue under (`#N` in the Backlog)."""
    return f"#{ref.number}" if not ref.host else ref.label(home)


def homed(sources, backlog):
    """`sources` with the workspace's Backlog as home when the cache has none yet, so its keys still resolve."""
    return replace(sources, home=sources.home or backlog)


def route(ref: IssueRef, source: bool = False) -> str:
    return (f"/issues/{ref.host}/{ref.repo}/{ref.number}" if ref.host else f"/issues/{ref.number}") + ("/source" if source else "")


def parse(path: str) -> tuple[IssueRef, bool] | None:
    """(identity, is the source page) of exactly one issue route, else None. `/issues/<n>` parses to the local
    namespace; the server turns that into the Backlog's issue."""
    parts = path.split("/")
    if parts[:2] != ["", "issues"]:
        return None
    source = parts[-1] == "source"
    *where, number = parts[2:len(parts) - source]
    if not number or not _NUMBER.fullmatch(number):
        return None
    if not where:
        return bare(int(number)), source
    host, *project = where
    if len(project) < 2 or (host == GITHUB and len(project) != 2) or not all(_SEGMENT.fullmatch(s) for s in where):
        return None
    return IssueRef("/".join(project), int(number), host), source


def filename(ref: IssueRef, source: bool = False) -> str:
    tail = "-source.html" if source else ".html"
    if not ref.host:
        return f"issue-{ref.number}{tail}"
    # The full digest of an unambiguous serialization: no slug runs two projects together, and the name stays bounded.
    return f"issue-q-{hashlib.sha256(json.dumps([ref.host, ref.repo, ref.number]).encode()).hexdigest()}{tail}"


def from_filename(name: str) -> IssueRef | None:
    """The local namespace's issue a numeric file name stands for. A hash is never read back: the caller passes the
    identity."""
    m = _FILE.fullmatch(name)
    return bare(int(m[1])) if m else None

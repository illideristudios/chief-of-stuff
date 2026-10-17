#!/usr/bin/env python3
"""A task's change as source and diff, `/issues/<n>/source` (Source48.dc.html, "Source & diff — task !48"): the change's
files with +additions −deletions, a NEW / CHANGED / REMOVED badge and the other open changes on the same file, then
each file's diff against the merge base, unified or split (a radio pair; CSS shows the one picked), with each review
thread inline under the line it is on. The diff is read from the `[graph]` table's clone, fetched as the
module graph fetches it; without the table the page lists the files from the sources cache. pages.py renders it on
request into `issue-<n>-source.html`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from pathlib import Path

import backlog
import board_sources
import decision_page
import module_graph
import store
from decision_page import HEAD
from fragment import href, tab_bar
from issue_page import _paths, _shared, anchor, inputs, put
from tracker import TRAILING_NUMBER, parse_tracker
from settings import Graph

HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
CACHE = ".diffs"
# Additions and deletions tint with the stage tokens, so the adopted dark scheme reaches them.
CSS = """<style id="source">
header .cmp{padding:6px 12px;text-align:right} header .cmp div{font:15px ui-monospace,monospace}
.src{display:grid;grid-template-columns:290px minmax(0,1fr);gap:12px;align-items:start}
ul.files{list-style:none;margin:0;padding:0;position:sticky;top:12px}
ul.files li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 8px;border-top:1px dotted var(--line);padding:7px 0}
ul.files a{font:12px ui-monospace,monospace;color:var(--fg);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
ul.files .d{font:11px ui-monospace,monospace;white-space:nowrap}
ul.files .b{grid-column:1/-1;display:flex;gap:4px}
.plus{color:var(--stage-review)} .minus{color:var(--dl)}
.chip.NEW{color:var(--stage-review);border:1.5px solid var(--stage-review)}
.chip.CHANGED{color:var(--stage-implement);border:1.5px solid var(--stage-implement)}
.chip.REMOVED{color:var(--stage-fix);border:1.5px dashed var(--stage-fix)}
.chip.also{color:var(--dl);border:1.5px dashed var(--dl)}
.diffs{display:flex;flex-direction:column;gap:12px;min-width:0}
.diffs .card{padding:0;overflow-x:auto}
.diffs h3{margin:0;padding:8px 14px;border-bottom:1px solid var(--line);font:600 13px ui-monospace,monospace}
table.diff{border-collapse:collapse;width:100%;font:12.5px/20px ui-monospace,SFMono-Regular,Menlo,monospace}
table.diff .hunk td{background:var(--ref-bg);color:var(--muted);padding:2px 14px;font-size:11.5px}
table.diff td.o,table.diff td.n{width:44px;color:var(--muted);text-align:right;padding-right:8px;font-size:11.5px;user-select:none}
table.diff td.sign{width:18px;text-align:center} table.diff td.tx{white-space:pre}
tr.add{background:color-mix(in srgb,var(--stage-review) 12%,transparent)} tr.add td.sign{color:var(--stage-review)}
tr.del{background:color-mix(in srgb,var(--dl) 12%,transparent)} tr.del td.sign{color:var(--dl)}
.seg{display:flex;align-self:flex-start;border:1px solid var(--line);border-radius:5px;overflow:hidden;background:var(--surface)}
.seg input{position:absolute;opacity:0} .seg label{font-size:13px;padding:4px 12px;cursor:pointer}
.seg label+input+label{border-left:1px solid var(--line)} .seg input:checked+label{background:var(--fg);color:var(--surface)}
.seg input:focus-visible+label{outline:2px solid var(--brass)}
table.split{display:none;border-collapse:collapse;width:100%;table-layout:fixed;font:12.5px/20px ui-monospace,SFMono-Regular,Menlo,monospace}
body:has(#layout-split:checked) table.split{display:table} body:has(#layout-split:checked) table.diff{display:none}
table.split .hunk td{background:var(--ref-bg);color:var(--muted);padding:2px 14px;font-size:11.5px}
table.split .lno,table.split .rno{width:40px;color:var(--muted);text-align:right;padding-right:8px;font-size:11.5px;user-select:none}
table.split .rno{border-left:1px solid var(--line)} table.split .left,table.split .right{white-space:pre;overflow:hidden;text-overflow:ellipsis}
td.left.del{background:color-mix(in srgb,var(--dl) 12%,transparent)} td.right.add{background:color-mix(in srgb,var(--stage-review) 12%,transparent)}
td.empty{background:repeating-linear-gradient(135deg,var(--ref-bg) 0 4px,var(--surface) 4px 8px)}
tr.note td{padding:4px 14px 8px 100px;font-family:"Alegreya Sans",system-ui,sans-serif}
.nt{background:var(--surface);border:1px solid var(--line);border-top:3px solid var(--stage-pr);border-radius:5px;padding:7px 12px;margin:4px 0;font-size:13px;line-height:1.45;white-space:normal}
.nt.resolved{border-top-color:var(--stage-review)} .nt p{color:var(--fg)}
@media (max-width:760px){.src{grid-template-columns:minmax(0,1fr)} ul.files{position:static}}
</style>
"""


GL_NOTES = ("discussions { nodes { resolvable resolved notes(first: 1) { nodes { author { username } body createdAt url "
            "position { filePath oldLine newLine } } } } }")
GH_NOTES = ("reviewThreads(first: 100) { nodes { isResolved path line diffSide "
            "comments(first: 1) { nodes { author { login } body createdAt url } } } }")


@dataclass(frozen=True)
class Note:
    path: str               # the file the thread is on
    line: int | None        # its line on that side; None when the forge gives none (outdated)
    side: str               # "new" or "old"
    thread: board_sources.ReviewThread


def notes(change: board_sources.Change, home, gh=backlog.run_gh, call=backlog._call) -> tuple[list[Note], str]:
    """The change's review threads with their file and line: (notes, error), from one small query for this change
    only, apart from the cache refresh's. Never raises."""
    when = board_sources._when
    try:
        n = int(change.ref[1:])
        if isinstance(home, backlog.GitHubBacklog):
            owner, name = home.repo.split("/", 1)
            q = (f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
                 f"{{ pullRequest(number: {n}) {{ {GH_NOTES} }} }} }}")
            code, out, err = gh(["api", "graphql", "-f", f"query={q}"])
            try:
                body = json.loads(out)
                nodes = body["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
            except (ValueError, KeyError, TypeError):
                return [], err.strip() or f"gh exited {code}"
            return [Note(t["path"], t.get("line"), "old" if t.get("diffSide") == "LEFT" else "new",
                         board_sources.ReviewThread(bool(t.get("isResolved")), (c.get("author") or {}).get("login") or "",
                                                    c.get("body") or "", when(c.get("createdAt")), c.get("url") or ""))
                    for t in nodes for c in ((t.get("comments") or {}).get("nodes") or [])[:1]], ""
        secret = backlog.token(home)
        if not secret:
            return [], home.missing_token
        q = (f"query {{ project(fullPath: {json.dumps(home.project)}) {{ mergeRequest(iid: {json.dumps(str(n))}) "
             f"{{ {GL_NOTES} }} }} }}")
        body, _, err = call("POST", f"{home.host.rstrip('/')}/api/graphql", secret, backlog.TIMEOUT, {"query": q})
        if err or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            return [], err or board_sources._graphql_errors(body if isinstance(body, dict) else {}) or "GitLab did not answer"
        out = []
        for d in (((body["data"].get("project") or {}).get("mergeRequest") or {}).get("discussions") or {}).get("nodes") or []:
            for c in ((d.get("notes") or {}).get("nodes") or [])[:1] if d.get("resolvable") else ():
                pos = c.get("position") or {}
                side = "new" if pos.get("newLine") is not None else "old"
                out.append(Note(pos.get("filePath") or "", pos.get(f"{side}Line"), side,
                                board_sources.ReviewThread(bool(d.get("resolved")), (c.get("author") or {}).get("username") or "",
                                                           c.get("body") or "", when(c.get("createdAt")), c.get("url") or "")))
        return out, ""
    except Exception as e:  # a forge answer shaped unlike its schema; the page still renders its diff
        return [], f"{type(e).__name__}: {e}"


def _note(n: Note, now: datetime) -> str:
    t = n.thread
    when = f" · {t.at.astimezone(now.tzinfo):%H:%M}" if t.at else ""
    return (f'<div class="nt{" resolved" if t.resolved else ""}"><span><b>{"Resolved" if t.resolved else "Open"} thread · '
            f'{escape(t.author)}{when}</b> · <a {href(t.url)}>thread</a></span><p>{escape(t.body)}</p></div>')


class File:
    def __init__(self, path: str, badge: str = ""):
        self.path, self.badge, self.add, self.dele, self.rows = path, badge, 0, 0, []


def parse(diff: str) -> list[File]:
    """`git diff` output as files: each with its badge, counts and rows (kind, old no, new no, text); a hunk header is
    ("hunk", "", "", header)."""
    files: list[File] = []
    old = new = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files.append(File(line.split(" b/", 1)[-1], "CHANGED"))
        elif not files:
            continue
        elif not files[-1].rows and not line.startswith("@@"):  # the file's header lines
            f = files[-1]
            if line.startswith("new file mode"):
                f.badge = "NEW"
            elif line.startswith("deleted file mode"):
                f.badge = "REMOVED"
            elif line.startswith("+++ b/"):
                f.path = line[6:]
            elif line.startswith("--- a/") and f.badge == "REMOVED":
                f.path = line[6:]
        elif m := HUNK.match(line):
            old, new = int(m[1]), int(m[2])
            files[-1].rows.append(("hunk", "", "", line))
        elif line.startswith("+"):
            files[-1].rows.append(("add", "", new, line[1:]))
            files[-1].add, new = files[-1].add + 1, new + 1
        elif line.startswith("-"):
            files[-1].rows.append(("del", old, "", line[1:]))
            files[-1].dele, old = files[-1].dele + 1, old + 1
        elif line.startswith(" "):
            files[-1].rows.append(("ctx", old, new, line[1:]))
            old, new = old + 1, new + 1
    return files


def diff(change: board_sources.Change, clone: Path, pages: Path) -> tuple[str, str]:
    """(merge base, `git diff` of the change's head against it), read once per head commit."""
    out = pages / CACHE / f"{change.head}.diff"
    if not out.is_file():
        base = module_graph.fetch(change, clone)
        # ponytail: the whole diff, however big; cap it per file if a change's page gets slow.
        text = module_graph.run(["git", "-C", str(clone), "diff", "--no-color", "--no-renames", "--no-ext-diff",
                                 "--end-of-options", base, change.head])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{base}\n{text}")
    base, _, text = out.read_text().partition("\n")
    return base, text


def _count(a: int, d: int) -> str:
    return " ".join(filter(None, (f'<span class="plus">+{a}</span>' if a else "",
                                  f'<span class="minus">−{d}</span>' if d else ""))) or "+0"


def _rows(f: File, mine: list[Note], now: datetime) -> tuple[str, list[Note]]:
    """The file's diff rows, each line followed by the notes on it; and the notes on no shown line."""
    out, left = [], list(mine)
    for kind, o, n, tx in f.rows:
        if kind == "hunk":
            out.append(f'<tr class="hunk"><td colspan="4">{escape(tx)}</td></tr>')
            continue
        sign = {"add": "+", "del": "−"}.get(kind, "")
        out.append(f'<tr class="{kind}"><td class="o">{o}</td><td class="n">{n}</td><td class="sign">{sign}</td>'
                   f'<td class="tx">{escape(tx)}</td></tr>')
        on = [x for x in left if x.line is not None and (x.line == n if x.side == "new" else x.line == o)]
        out += [f'<tr class="note"><td colspan="4">{_note(x, now)}</td></tr>' for x in on]
        left = [x for x in left if x not in on]
    return "\n".join(out), left


def _change(ref: str, sources: board_sources.Sources) -> board_sources.Change | None:
    """The change naming the issue: an open one first, then a merged one."""
    order = {"open": 0, "merged": 1}
    return min((c for c in sources.changes.values() if ref in c.issues), key=lambda c: order.get(c.state, 2), default=None)


def _split(f: File, mine: list[Note], now: datetime) -> str:
    """The old side left, the new side right; within a change, removed and added lines pair row by row. A note follows
    the row holding its line."""
    def side(row, cls: str) -> str:
        if row is None:
            return f'<td class="{cls[0]}no"></td><td class="{cls} empty"></td>'
        kind, o, n, tx = row
        return f'<td class="{cls[0]}no">{o if cls == "left" else n}</td><td class="{cls} {kind}">{escape(tx)}</td>'

    out, dels, adds = [], [], []

    def row(left, right) -> None:
        out.append("<tr>" + side(left, "left") + side(right, "right") + "</tr>")
        out.extend(f'<tr class="note"><td colspan="4">{_note(x, now)}</td></tr>' for x in mine if x.line is not None and (
            (x.side == "old" and left and x.line == left[1]) or (x.side == "new" and right and x.line == right[2])))

    def flush():
        for k in range(max(len(dels), len(adds))):
            row(dels[k] if k < len(dels) else None, adds[k] if k < len(adds) else None)
        dels.clear(), adds.clear()

    for r in f.rows:
        if r[0] == "del":
            if adds:
                flush()
            dels.append(r)
        elif r[0] == "add":
            adds.append(r)
        else:
            flush()
            if r[0] == "hunk":
                out.append(f'<tr class="hunk"><td colspan="4">{escape(r[3])}</td></tr>')
            else:
                row(r, r)
    flush()
    return "\n".join(out)


LAYOUT = ('  <div class="seg" role="group" aria-label="Diff layout">'
          '<input type="radio" name="layout" id="layout-unified" checked><label for="layout-unified">Unified</label>'
          '<input type="radio" name="layout" id="layout-split"><label for="layout-split">Split</label></div>\n')


def render(number: int, trackers: list[tuple[date, str]], sources: board_sources.Sources, now: datetime,
           graph: Graph | None = None, pages: Path | None = None, root: Path | None = None,
           notes: list[Note] | None = None, notes_error: str = "", pending: int = 0,
           navigation: dict | None = None) -> str | None:
    """The page, or None when no task in `trackers` or the store names issue `number`. `notes` are the change's review threads."""
    ref = f"#{number}"
    mine = [t for _, text in trackers for t in parse_tracker(text).tasks
            if (m := TRAILING_NUMBER.search(t.issue)) and int(m[1]) == number]
    store_item = None
    if not mine and root is not None and (root / "store.db").is_file():
        db = store.Store(root / "store.db")
        try:
            store_item = next(
                (i for i in db.all_items() if (m := TRAILING_NUMBER.search(i.issue_key)) and int(m[1]) == number),
                None,
            )
        finally:
            db.engine.dispose()
    if not mine and store_item is None:
        return None
    issue = sources.issues.get(ref) or (sources.issues.get(store_item.issue_key) if store_item else None)
    name = issue.title if issue else (mine[0].label if mine else (store_item.title or ref))
    change = _change(ref, sources)
    crumb = f"Source · {escape(change.ref) + ' · ' if change else ''}{ref} {escape(name)}"
    title = f"{escape(change.ref)} · {escape(change.title)}" if change else escape(name)
    compare = note = ""
    files: list[File] = []
    if change:
        if graph and pages and root:
            try:
                base, text = diff(change, root / graph.clone, pages)
                files = parse(text)
                compare = f"{escape(change.base)}@{base[:7]} → {change.head[:7]}"
            except (OSError, ValueError) as e:
                note = f"The diff is unavailable: {escape(str(e))}"
        else:
            note = "The diff needs the clone: the settings have no <code>[graph]</code> table."
        if not files:  # the cache's list, without the diff's badges
            stats = {s.path: s for s in change.stats}
            for p in _paths(change):
                files.append(File(p))
                if s := stats.get(p):
                    files[-1].add, files[-1].dele = s.additions, s.deletions
    shared = _shared(change, list(sources.changes.values())) if change else {}

    items = []
    for f in files:
        chips = (f'<span class="chip {f.badge}">{f.badge}</span>' if f.badge else "") + "".join(
            f'<span class="chip also">ALSO {escape(r)}</span>' for r in shared.get(f.path, ()))
        items.append(f'      <li><a href="#{escape(anchor(f.path))}">{escape(f.path)}</a><span class="d">{_count(f.add, f.dele)}</span>'
                     f'<span class="b">{chips}</span></li>')
    tables, notes = [], list(notes or ())
    for f in files:
        if not f.rows:
            continue
        on = [x for x in notes if x.path == f.path]
        rows, off = _rows(f, on, now)
        notes = [x for x in notes if x.path != f.path]
        tables.append(f'    <div class="card"><h3>{escape(f.path)}</h3>\n' + "".join(_note(x, now) for x in off)
                      + f'    <table class="diff" id="{escape(anchor(f.path))}" data-path="{escape(f.path)}">\n{rows}\n    </table>\n'
                      f'    <table class="split" data-split-path="{escape(f.path)}">\n{_split(f, [x for x in on if x not in off], now)}\n    </table></div>')
    if notes:  # threads on files the page shows no diff of
        tables.insert(0, '    <div class="card">' + "".join(_note(x, now) for x in notes) + "</div>")
    if notes_error:
        tables.insert(0, f'    <p class="card">Review notes unavailable: {escape(notes_error)}</p>')
    if not change:
        body = f'  <p class="card">Nothing to diff: no change names {ref} yet.</p>\n'
        if issue and issue.body:
            body += f'  <div class="card"><pre>{escape(issue.body)}</pre></div>\n'
    else:
        total = f"{len(files)} · +{sum(f.add for f in files)} −{sum(f.dele for f in files)}"
        body = (LAYOUT if tables else "") + (f'  <div class="src">\n    <div class="card"><div class="cap">files · {total}</div>\n'
                f'    <ul class="files">\n' + "\n".join(items) + "\n    </ul></div>\n"
                f'    <div class="diffs">\n' + (f'    <p class="card">{note}</p>\n' if note else "")
                + "\n".join(tables) + "\n    </div>\n  </div>\n")
    cmp = f'    <div class="card cmp"><span class="cap">compare</span><div>{compare}</div></div>\n' if compare else ""
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>Source {escape(change.ref) if change else ref}</title>\n{HEAD}{CSS}</head>\n"
            f'<body>\n<main class="source">\n{tab_bar("Board", pending, True, **(navigation or {}))}  <header class="top">\n    <div>\n      <span class="eyebrow">{crumb} · '
            f'<a href="/issues/{number}">task</a> · <a href="/">board</a></span>\n'
            f"      <h1>{title}</h1>\n    </div>\n{cmp}  </header>\n{body}</main>\n</body>\n</html>\n")


def write(root: Path, pages_dir: Path, number: int) -> Path | None:
    """Render `issue-<number>-source.html`; with no page, remove the file and return None."""
    cfg, now, trackers, settings = inputs(root)
    sources = board_sources.load(pages_dir)
    change = _change(f"#{number}", sources)
    # ponytail: one small forge query per render (about once a minute while the page is open); cache by head if it bites.
    found, error = notes(change, cfg.backlog) if change and cfg.backlog else ([], "")
    ctx = decision_page.context(root, now.date().isoformat())
    page = render(number, trackers, sources, now, settings.graph, pages_dir, root, found, error,
                  decision_page.pending_count(pages_dir, ctx), ctx.navigation if ctx else None)
    return put(pages_dir / f"issue-{number}-source.html", page)

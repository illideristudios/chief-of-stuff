#!/usr/bin/env python3
"""One tracker issue's page, `/issues/<n>` (Task.dc.html, "Task page — laptop"), from the trackers and the cached
forge sources: the header; its merge request (merge order, overlap, pipeline jobs, conflicts, threads); the issue's
one Flow row; What happens next; Files changed; Architecture (module_graph.py, as on the decision page); How it got here
(its Log lines); Review; Acceptance (the bullets under the issue body's "Acceptance" heading); its workers; and
References. pages.py renders it on request into `issue-<n>.html`.
"""

from __future__ import annotations

import re
from collections.abc import Container
from dataclasses import replace
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import board_sources
import flow_chart
import tracker_log
from decision_page import HEAD, _md, _section, context, entries, held_refs, span, write_if_changed
from fragment import href, tab_bar
from module_graph import section as _graph
from clock import dur as _dur
from task_forge import forge_ends, held_names, hold_causes
from tracker import TRAILING_NUMBER, issue_number, parse_tracker, resolve_due as _resolve_due
from workspace import Config, ConfigError, daily_trackers, read_config
from settings import Graph, Kanban, SettingsError, load as load_settings

# A worker's outcome by its last launcher move's stage (tracker_log.moves: completed → pr, NEEDS INPUT (and historical HUMAN REVIEW) → review).
ENDED = {"pr": "completed", "review": "needs input"}
# The Flow chart's colours on HEAD's tokens in the dark scheme, as the board sets them (render_board.render).
CSS = """<style>
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) :is(.gantt,.gantt-legend){--gantt-ink:var(--fg);--gantt-muted:var(--muted);--gantt-bg:var(--bg);--gantt-rule:var(--line);--gantt-edge:var(--line);--gantt-grid:var(--line);--gantt-past:var(--surface);--gantt-now:var(--dl);--gantt-hold:var(--dl)}}
:root[data-theme="dark"] :is(.gantt,.gantt-legend){--gantt-ink:var(--fg);--gantt-muted:var(--muted);--gantt-bg:var(--bg);--gantt-rule:var(--line);--gantt-edge:var(--line);--gantt-grid:var(--line);--gantt-past:var(--surface);--gantt-now:var(--dl);--gantt-hold:var(--dl)}
.meta{color:var(--muted);font-size:12px}
header .chip.running{color:var(--surface);background:var(--stage-implement);border-color:var(--stage-implement)}
.chk,.wk{display:grid;grid-template-columns:96px 74px minmax(0,1fr);gap:0 10px;align-items:baseline;padding:6px 0;font-size:13px}
.wk{grid-template-columns:minmax(0,1fr) 64px auto;padding:5px 0}
.chk+.chk,.wk+.wk{border-top:1px dotted var(--line)}
.mr .chip{border:1px solid var(--line)}
.mk{font-size:10.5px;padding:0 6px;border-radius:3px;border:1px solid var(--line);justify-self:start;white-space:nowrap}
.mk.passed{border-color:#009E73} .mk.failed{border:1px dashed var(--dl);color:var(--dl)}
.wk .st{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)} .wk .st.running{color:var(--stage-implement)}
.fl{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:0 10px;padding:3px 0;font-size:13px}
.fl .path{font-family:ui-monospace,monospace;overflow-wrap:anywhere} .fl .d{font-variant-numeric:tabular-nums;color:var(--muted)}
.fl.total{border-top:1px solid var(--line);margin-top:4px;padding-top:6px}
.rv{display:grid;grid-template-columns:74px minmax(0,1fr);gap:2px 10px;padding:6px 0;font-size:13px}
.rv+.rv{border-top:1px dotted var(--line)} .rv p{grid-column:2;margin:0}
</style>
"""
ACCEPTANCE = re.compile(r"(?im)^#{1,6}[ \t]*acceptance\b.*$")
HEADING = re.compile(r"^\s*#{1,6}\s")
BULLET = re.compile(r"^\s*[-*+]\s+(?:\[([ xX])\]\s*)?(.*\S)")


def _clock(at: datetime, now: datetime) -> str:
    return f"{at:%H:%M}" if at.date() == now.date() else f"{at:%a %H:%M}"


def _number(c: board_sources.Change) -> tuple[str, int]:
    """(repo, N): `!N`/`#N` is this repo's, `owner/repo#N` another's."""
    m = TRAILING_NUMBER.search(c.ref)
    return c.ref[:m.start()], int(m[1])


def _queue(c: board_sources.Change, changes) -> tuple[int, int, list[str]] | None:
    """(K, M, the refs merging before it) among its repo's open changes into c's base, by number; None unless c is open.
    ponytail: only the changes the sources cache knows (those the trackers name); list the base's open changes if
    M must count the whole forge."""
    if c.state != "open":
        return None
    line = sorted((o for o in changes if o.state == "open" and o.base == c.base and o.ref != c.ref
                   and _number(o)[0] == _number(c)[0]), key=_number)
    before = [o.ref for o in line if _number(o) < _number(c)]
    return len(before) + 1, len(line) + 1, before


def _after(before: list[str]) -> str:
    return f"after {', '.join(before)}" if before else "next in line"


def _shared(c: board_sources.Change, changes) -> dict[str, list[str]]:
    """Each of c's files that another open change also touches, with those changes' refs."""
    others = [o for o in changes if o.state == "open" and o.ref != c.ref]
    return {p: refs for p in _paths(c) if (refs := [o.ref for o in others if p in o.files])}


def _paths(c: board_sources.Change) -> tuple[str, ...]:
    return c.files or tuple(f.path for f in c.stats)


def _change(c: board_sources.Change, changes, now: datetime) -> str:
    kind, word = ("MR", "Merge request") if c.ref.startswith("!") else ("PR", "Pull request")
    state = "draft" if c.draft and c.state == "open" else c.state
    merged = f" {c.merged_at.astimezone(now.tzinfo):%H:%M}" if c.merged_at else ""  # (#200) the page's zone, not UTC
    rows = [("pipeline", c.pipeline or "none", c.pipeline or "", "")]
    rows += [("job", j.status, j.status, escape(j.name) + (f" · {span_s(j.seconds)}" if j.seconds is not None else ""))
             for j in c.jobs]
    rows.append(("approvals", str(c.approvals), "", "approved" if c.approved else ""))
    if q := _queue(c, changes):
        rows.append(("merge order", f"{q[0]} of {q[1]}", "", escape(_after(q[2]))))
    shared = _shared(c, changes)
    rows.append(("overlap", f"{len(shared)} file{'s' * (len(shared) != 1)}" if shared else "none", "",
                 "; ".join(f"{escape(p)} with {escape(', '.join(refs))}" for p, refs in shared.items())))
    rows.append(("conflicts", "yes" if c.conflicts else "none", "failed" if c.conflicts else "", ""))
    resolved = sum(t.resolved for t in c.threads)
    rows.append(("threads", f"{len(c.threads) - resolved} open", "", f"{resolved} resolved"))
    body = "\n".join(f'      <div class="chk"><span class="cap">{cap}</span><span class="mk {escape(cls)}">{escape(mark)}</span>'
                     f"<span>{note}</span></div>" for cap, mark, cls, note in rows)
    return (f'  <section>\n    <h2>{word} · <a class="ref" data-k="{kind}" {href(c.url)}>{escape(c.ref)}</a></h2>\n'
            f'    <div class="card mr">\n      <strong>{escape(c.title)}</strong> <span class="chip">{escape(state.upper())}{merged}</span>'
            f' <span class="meta">→ {escape(c.base)}</span>\n{body}\n    </div>\n  </section>\n')


def span_s(seconds: int) -> str:
    return f"{seconds}s" if seconds < 60 else span(timedelta(seconds=seconds))


def anchor(path: str) -> str:
    """The id of a file's diff on the source page, which the task page's file links point at."""
    return "f-" + re.sub(r"[^A-Za-z0-9_.-]", "-", path)


def _files(c: board_sources.Change, changes, number: int) -> str:
    """FILES CHANGED: a link to the source page; each file, linked to its diff there, its +additions −deletions when
    known, "also !N" for another open change on it."""
    stats, shared = {f.path: f for f in c.stats}, _shared(c, changes)
    rows = [f'      <div class="fl"><span class="cap">files changed</span><a href="/issues/{number}/source">open source &amp; diff</a>'
            "<span></span></div>"]
    for p in _paths(c):
        f = stats.get(p)
        rows.append(f'      <div class="fl"><a class="path" href="/issues/{number}/source#{escape(anchor(p))}">{escape(p)}</a>'
                    f'<span class="d">{f"+{f.additions} −{f.deletions}" if f else ""}</span>'
                    f'<span class="meta">{"also " + escape(", ".join(shared[p])) if p in shared else ""}</span></div>')
    if c.stats:
        rows.append(f'      <div class="fl total"><span class="cap">total</span><span class="d">+{sum(f.additions for f in c.stats)} '
                    f'−{sum(f.deletions for f in c.stats)}</span><span></span></div>')
    return _section(f"Files changed · {len(rows) - 1 - bool(c.stats)}", '    <div class="card">\n' + "\n".join(rows) + "\n    </div>")


def _review(c: board_sources.Change, now: datetime) -> str:
    """REVIEW: open threads, then resolved, each newest first."""
    if not c.threads:
        return _section("Review", f'    <div class="card"><p>No review threads on {escape(c.ref)}.</p></div>')
    newest = sorted(c.threads, key=lambda t: t.at.timestamp() if t.at else 0, reverse=True)
    rows = []
    for t in sorted(newest, key=lambda t: t.resolved):
        when = f" · {_clock(t.at.astimezone(now.tzinfo), now)}" if t.at else ""
        link = f' · <a {href(t.url)}>thread</a>' if t.url else ""
        rows.append(f'      <div class="rv"><span class="mk{" passed" if t.resolved else ""}">{"Resolved" if t.resolved else "Open"}</span>'
                    f"<span><b>{escape(t.author)}</b>{when}{link}</span><p>{escape(t.body)}</p></div>")
    resolved = sum(t.resolved for t in c.threads)
    return _section(f"Review · {len(c.threads) - resolved} open · {resolved} resolved",
                    '    <div class="card">\n' + "\n".join(rows) + "\n    </div>")


def _acceptance(issue: board_sources.Issue | None) -> str:
    """ACCEPTANCE: the bullets under the body's "Acceptance" heading, up to the next heading; `[x]` is met."""
    m = ACCEPTANCE.search(issue.body) if issue else None
    met = []
    for line in (issue.body[m.end():].splitlines() if m else []):
        if HEADING.match(line):
            break
        if b := BULLET.match(line):
            met.append((b[1] in ("x", "X"), b[2]))
    if not met:
        return _section("Acceptance", '    <div class="card"><p>No acceptance criteria in the issue.</p></div>')
    rows = "\n".join(f'      <div class="chk"><span class="mk{" passed" if ok else ""}">{"met" if ok else "not met"}</span>'
                     f"<span>{_md(what)}</span></div>" for ok, what in met)
    return _section(f"Acceptance · {sum(ok for ok, _ in met)} of {len(met)} met", f'    <div class="card">\n{rows}\n    </div>')


def _next(rows, change: board_sources.Change | None, changes, why_not: str, now: datetime) -> str:
    """WHAT HAPPENS NEXT: the Flow row's forecast stages and the change's place in its merge queue."""
    if why_not:
        lines = [f"Nothing happens next: {why_not}."]
    else:
        lines = [f"{escape(g.category)} ~{_clock(g.start.astimezone(now.tzinfo), now)}–{_clock(g.end.astimezone(now.tzinfo), now)}"
                 for _, r in rows for g in r.segments if g.kind == "forecast"]
        lines = lines or ["No estimate: the task has no due time to forecast from."]
        if change and (q := _queue(change, changes)):
            lines.append(f"{escape(change.ref)} merge queue {q[0]} of {q[1]} into {escape(change.base)}, {escape(_after(q[2]))}")
    return _section("What happens next", '    <div class="card">\n' + "\n".join(f"      <p>{x}</p>" for x in lines) + "\n    </div>")


def render(number: int, trackers: list[tuple[date, str]], sources: board_sources.Sources, now: datetime,
           lanes: dict | None = None, cfg: Config | None = None, graph: Graph | None = None, pages: Path | None = None,
           root: Path | None = None, pending: int = 0, kanban: Kanban | None = None,
           pending_refs: Container[str] = frozenset()) -> str | None:
    """The page, or None when no task in `trackers` names issue `number`. `cfg` resolves a task's due (the board's
    deadlines); `graph`, `pages` and `root` draw Architecture as the decision page draws its module graph."""
    ref = f"#{number}"
    names = lambda t: {k.strip().casefold() for k in (t.name, t.item) if k.strip()}  # noqa: E731
    known = [t for _, text in trackers for t in parse_tracker(text).tasks]
    mine = [t for t in known if issue_number(t.issue) == number]
    if not mine:
        return None
    keys = set().union(*map(names, mine))
    log = sorted((m for day, text in trackers for m in tracker_log.moves(text, day, now.tzinfo)), key=lambda m: m.at)
    # A forecast runs to the task's due, as the board's Flow chart feeds estimate.task_end's due.
    cfg = cfg or Config("", "", "", getattr(now.tzinfo, "key", "UTC"), (), None, None)
    ends = {t.item: end for t in parse_tracker(trackers[-1][1]).tasks
            if t.kind != "done" and (end := _resolve_due(t.due, cfg, now.date()))}
    # (#200, #220) forge_ends is shared with the board, so both end a row at the same time
    # #228: held as the board holds it, from today's tasks less the standing ones, so both name the same cause.
    today = [t for t in parse_tracker(trackers[-1][1]).tasks if not t.standing]
    holds = hold_causes(today, kanban, sources, lanes or {}, pending_refs)
    held = held_names(holds)
    rows = [(s, r) for s, r in flow_chart.build(log, known, lanes or {}, held, ends, now,
                                                ended=forge_ends(known, sources, now.tzinfo))
            if issue_number(r.ref) == number]
    log = [m for m in log if m.name.strip().casefold() in keys]
    issue, latest = sources.issues.get(ref), {k: t for t in known for k in names(t)}  # later trackers win
    status = rows[0][0] if rows else mine[-1].kind
    if status not in ("merged", "closed", "done") and any(t in holds for t in mine):
        status = "needs input"
    name = rows[0][1].name if rows else mine[0].label

    order = {"open": 0, "merged": 1}
    changes = sorted((c for c in sources.changes.values() if ref in c.issues), key=lambda c: order.get(c.state, 2))
    every = list(sources.changes.values())
    cards = "".join(_change(c, every, now) for c in changes) or _section("Change", f'    <div class="card"><p>No change names {ref} yet.</p></div>')
    change = changes[0] if changes else None
    current = {t.name.strip().casefold(): t for t in mine}.values()
    why_not = ("the issue is closed" if issue and issue.state == "closed" else
               "the task is done" if status == "merged" or all(t.kind == "done" for t in current) else "")
    ahead = _next(rows, change, every, why_not, now) + (_files(change, every, number) if change and _paths(change) else "")
    ahead += _graph(change, graph, pages, root, "Architecture", f"No change names {ref} to graph.")

    task_of = lambda m: (t.name.strip() if (t := latest.get(m.name.strip().casefold())) else m.name).casefold()  # noqa: E731
    workers: dict[str, list[list]] = {}  # who → [[started move, its Log match, ended move or None], ...]
    what = {}  # a launcher move → its compact How-it-got-here text
    for m in log:
        if not m.launch:
            continue
        if started := tracker_log.STARTED_DETAIL.match(m.line):
            stage = next((x.stage for x in reversed(log) if not x.launch and x.at <= m.at and task_of(x) == task_of(m)), m.stage)
            workers.setdefault(started[2], []).append([replace(m, stage=stage), started, None])
            what[m] = f"{escape(stage)} started · {escape(started[2])} · {escape(started[4])}"
        elif (w := tracker_log.ENDED.match(m.line)) and (runs := workers.get(w[3])):
            # ponytail: an end pairs with a start on the same day's tracker (tracker_log.moves); a run over midnight
            # loses its end and counts nothing, pair across days if that shows up.
            runs[-1][2] = m
            what[m] = f"ended {escape(ENDED.get(m.stage, 'ended'))} · {escape(w[3])} · {escape(runs[-1][1][4])}"
    steps = "\n".join(f'      <div class="step"><span class="when">{_clock(m.at, now)}</span><span class="dot" data-k="{escape(m.stage)}">'
                      f"</span><span>{what.get(m) or _md(m.line.split(' ', 2)[2])}</span></div>" for m in log)
    # (#200, #220) the same clipped rows end both a still-running worker's span and elapsed, below
    end = max((g.end for _, r in rows for g in r.segments if g.kind == "done"), default=now)
    lines, spans = [], []
    for who, runs in workers.items():
        task = latest.get(runs[-1][0].name.strip().casefold())
        live = runs[-1][2] is None and task is not None and task.kind == "running" and task.owner.strip() == who
        state = "running" if live else ENDED.get(runs[-1][2].stage, "ended") if runs[-1][2] else "ended"
        stop = min(now, end)
        mine_spans = [(s.at, e.at if e else stop) for s, _, e in runs if e or (live and s is runs[-1][0])]
        spans += mine_spans
        stages = ", ".join(dict.fromkeys(s.stage for s, _, _ in runs))
        took = sum((e - s for s, e in mine_spans), timedelta())
        lines.append((took, f'      <div class="wk"><span><b>{escape(who)}</b> · one-shot{f" ×{len(runs)}" if len(runs) > 1 else ""} · '
                            f'{escape(runs[-1][1][3])} · {escape(stages)}</span><span class="when">{_dur(took)}</span>'
                            f'<span class="st{" running" if live else ""}">{escape(state)}</span></div>'))
    # Worked: the union of every launch's interval, so overlapping workers count once.
    first = min((s for s, _ in spans), default=now)
    worked, reach = timedelta(), first
    for s, e in sorted(spans):
        worked, reach = worked + max(timedelta(), e - max(s, reach)), max(reach, e)
    elapsed = end - first
    worked_line = (f'<p class="meta">Worked {_dur(worked)} of {_dur(elapsed)} elapsed · {_dur(elapsed - worked)} waiting</p>\n'
                   if spans else "")

    workers_html = (_section(f"Workers · {_dur(sum((t for t, _ in lines), timedelta()))} · {len(lines)} sessions",
                             '    <div class="card">\n' + "\n".join(x for _, x in lines) + "\n    </div>") if lines else
                    _section("Workers", '    <div class="card"><p>No workers launched.</p></div>'))

    refs = [("issue", f'<a {href(issue.url)}>{ref} {escape(issue.title)}</a>' if issue else f"{ref} {escape(name)}",
             issue.state if issue else "")]
    refs += [("MR" if c.ref.startswith("!") else "PR", f'<a {href(c.url)}>{escape(c.ref)} {escape(c.title)}</a>',
              " · ".join(filter(None, (c.state, c.pipeline)))) for c in changes]
    refs_html = "\n".join(f'      <span class="cap">{cap}</span><span>{what}</span><span class="st">{escape(st)}</span>' for cap, what, st in refs)

    lane = next((t.lane.strip() for t in reversed(mine) if t.lane.strip()), "")
    eyebrow = " · ".join(["Task", f'<a class="ref" data-k="issue" {href(issue.url)}>{ref}</a>' if issue else ref,
                          *([f"lane {escape(lane)}"] if lane else []), '<a href="/">board</a>'])  # (#215) no render clock
    flow = flow_chart.section(rows, now)
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{ref} {escape(name)}</title>\n{HEAD}<style>\n{flow_chart.css() if flow else ''}</style>\n{CSS}</head>\n"
            f'<body>\n<main class="issue">\n{tab_bar("Board", pending, True)}  <header class="top">\n    <div>\n      <span class="eyebrow">{eyebrow}</span>\n'
            f"      <h1>{escape(name)}</h1>\n    </div>\n"
            f'    <span class="chip {escape(status.replace(" ", "-"))}">{escape(status.upper())}</span>\n  </header>\n\n'
            + cards + (f"  <section>\n{flow}{worked_line}  </section>\n" if flow else "") + ahead
            + '  <div class="cols">\n  <div class="col">\n'
            + _section("How it got here", f'    <div class="card timeline">\n{steps}\n    </div>' if steps else '    <div class="card"><p>No Log lines yet.</p></div>')
            + (_review(change, now) if change else "")
            + '  </div>\n  <div class="col">\n' + _acceptance(issue)
            + workers_html
            + _section("References", f'    <div class="card refs">\n{refs_html}\n    </div>')
            + "  </div>\n  </div>\n</main>\n</body>\n</html>\n")


def inputs(root: Path):
    """(cfg, now, trackers the Flow chart reaches back over, settings) for a page of one issue."""
    cfg = read_config(root)
    now = datetime.now(cfg.zone).replace(microsecond=0)
    reach = (now - flow_chart.WINDOWS[-1][1]).date()
    trackers = [(d, path.read_text()) for d, path in daily_trackers(root, cfg) if reach <= d <= now.date()]
    try:
        return cfg, now, trackers, load_settings(root, cfg.settings_path)
    except SettingsError as e:
        raise ConfigError(str(e)) from None


def put(out: Path, page: str | None) -> Path | None:
    """Write the page, or remove the file and return None when there is none."""
    if page is None:
        out.unlink(missing_ok=True)
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    write_if_changed(out, page)
    return out


def write(root: Path, pages_dir: Path, number: int) -> Path | None:
    """Render `issue-<number>.html`; with no page, remove the file and return None. The page server calls this in
    process."""
    cfg, now, trackers, settings = inputs(root)
    ctx = context(root, now.date().isoformat())
    pending = entries(pages_dir, ctx) if ctx else []
    page = render(number, trackers, board_sources.load(pages_dir), now, settings.lanes, cfg, settings.graph, pages_dir, root,
                  sum(d is not None for _, d, _, _ in pending), settings.kanban, held_refs(pending, ctx))
    return put(pages_dir / f"issue-{number}.html", page)

#!/usr/bin/env python3
"""One tracker issue's page, `/issues/<n>` (Task.dc.html, "Task page — laptop"), from the trackers and the cached
forge sources: the header; its merge request (merge order, overlap, pipeline jobs, conflicts, threads); the issue's
one Flow row; What happens next; Files changed; Architecture (module_graph.py, as on the decision page); How it got here
(its Log lines); Review; Acceptance (the bullets under the issue body's "Acceptance" heading); its decisions, each
linked to its page; its workers; and References. pages.py renders it on request into `issue-<n>.html`.
"""

from __future__ import annotations

import re
from collections.abc import Container
from dataclasses import replace
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import board_sources
from _vendor.markdown_it import MarkdownIt
import flow_chart
from backlog_ref import Backlog, GitHubBacklog, home_of, issue_ref, store_label
from clock import parse_stamp
from task_forge import (EXHAUSTED_CHIP, EXHAUSTED_UNBLOCK, PageData, change_issue_keys, lookup_keys,
                        source_key, source_worker_tasks, tracker_issue_tasks)
import tracker_log
from decision_actions import addresses, state_of
from decision_page import HEAD, Linker, _section, context, entries, held_refs, span, store_decision_badge, write_if_changed
from fragment import external_url, href, tab_bar, issue_href, issue_page_name, latest_thread_link
from page_common import drop_html_comments, markdown_blocks, markdown_inline
from module_graph import section as _graph
from clock import dur as _dur
from task_forge import forge_ends, held_names, hold_causes
from tracker import TRAILING_NUMBER, Task, issue_key, issue_number, parse_tracker, resolve_due as _resolve_due
from workspace import Config, ConfigError, EngineStatus, daily_trackers, engine_health, read_config, read_engine_status
from settings import Graph, Kanban, SettingsError, load as load_settings

# A worker's outcome by its last launcher move's stage (tracker_log.moves: completed → pr, HUMAN REVIEW → review).
ENDED = {"pr": "completed", "review": "needs input"}
# The Flow chart's colours on HEAD's tokens in the dark scheme, as the board sets them (render_board.render).
CSS = """<style>
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) :is(.gantt,.gantt-legend){--gantt-ink:var(--fg);--gantt-muted:var(--muted);--gantt-bg:var(--bg);--gantt-rule:var(--line);--gantt-edge:var(--line);--gantt-grid:var(--line);--gantt-past:var(--surface);--gantt-now:var(--dl);--gantt-hold:var(--dl)}}
:root[data-theme="dark"] :is(.gantt,.gantt-legend){--gantt-ink:var(--fg);--gantt-muted:var(--muted);--gantt-bg:var(--bg);--gantt-rule:var(--line);--gantt-edge:var(--line);--gantt-grid:var(--line);--gantt-past:var(--surface);--gantt-now:var(--dl);--gantt-hold:var(--dl)}
.meta{color:var(--muted);font-size:12px}
.mono{font-family:ui-monospace,monospace;font-size:12px}
header .chip.running{color:var(--surface);background:var(--stage-implement);border-color:var(--stage-implement)}
header .chip.needs-input,header .chip.retries-exhausted{color:var(--dl);border-color:var(--dl)}
.engine-warn{border:1px solid var(--dl);border-radius:3px;padding:6px 10px;color:var(--dl);font-size:12.5px;margin:0 0 12px}
.chk,.wk{display:grid;grid-template-columns:96px 74px minmax(0,1fr);gap:0 10px;align-items:baseline;padding:6px 0;font-size:13px}
.wk{grid-template-columns:minmax(0,1fr) 64px auto;padding:5px 0}
.chk+.chk,.wk+.wk{border-top:1px dotted var(--line)}
.unread>.why+.chk{margin-top:4px}
.mr .chip{border:1px solid var(--line)}
.mk{font-size:10.5px;padding:0 6px;border-radius:3px;border:1px solid var(--line);justify-self:start;white-space:nowrap}
.mk.passed{border-color:#009E73} .mk.failed{border:1px dashed var(--dl);color:var(--dl)}
.wk .st{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)} .wk .st.running{color:var(--stage-implement)}
.fl{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:0 10px;padding:3px 0;font-size:13px}
.fl .path{font-family:ui-monospace,monospace;overflow-wrap:anywhere} .fl .d{font-variant-numeric:tabular-nums;color:var(--muted)}
.fl.total{border-top:1px solid var(--line);margin-top:4px;padding-top:6px}
.rv{display:grid;grid-template-columns:74px minmax(0,1fr);gap:2px 10px;padding:6px 0;font-size:13px}
.rv+.rv{border-top:1px dotted var(--line)} .rv>.prose{grid-column:2;margin:0}
</style>
"""


def _clock(at: datetime, now: datetime) -> str:
    return f"{at:%H:%M}" if at.date() == now.date() else f"{at:%a %H:%M}"


def _step_when(at: datetime, now: datetime) -> str:
    """Weekday and time for a history step. Today's bare HH:MM is read as the previous step's day (#998)."""
    if at.tzinfo is not None and now.tzinfo is not None:
        at = at.astimezone(now.tzinfo)
    return f"{at:%a %H:%M}"


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
    c_url = external_url(c.url)
    ref_html = (f'<a class="ref" data-k="{kind}" {href(c_url)}>{escape(c.ref)}</a>' if c_url
                else f'<span class="ref" data-k="{kind}">{escape(c.ref)}</span>')
    b_url = external_url(c.branch_url)
    branch_html = (f'<a class="mono" {href(b_url)}>{escape(c.branch)}</a> → ' if c.branch and b_url
                   else f'<span class="mono">{escape(c.branch)}</span> → ' if c.branch else "→ ")
    return (f'  <section>\n    <h2>{word} · {ref_html}</h2>\n'
            f'    <div class="card mr">\n      <strong>{escape(c.title)}</strong> <span class="chip">{escape(state.upper())}{merged}</span>'
            f' <span class="meta">{branch_html}{escape(c.base)}</span>\n{body}\n    </div>\n  </section>\n')


def span_s(seconds: int) -> str:
    return f"{seconds}s" if seconds < 60 else span(timedelta(seconds=seconds))


def anchor(path: str) -> str:
    """The id of a file's diff on the source page, which the task page's file links point at."""
    return "f-" + re.sub(r"[^A-Za-z0-9_.-]", "-", path)


def _files(c: board_sources.Change, changes, number: int | str) -> str:
    """FILES CHANGED: a link to the source page; each file, linked to its diff there, its +additions −deletions when
    known, "also !N" for another open change on it."""
    source_url = str(number) if str(number).startswith("/") else f"/issues/{number}/source"
    stats, shared = {f.path: f for f in c.stats}, _shared(c, changes)
    rows = [f'      <div class="fl"><span class="cap">files changed</span><a {href(source_url)}>open source &amp; diff</a>'
            "<span></span></div>"]
    for p in _paths(c):
        f = stats.get(p)
        rows.append(f'      <div class="fl"><a class="path" {href(f"{source_url}#{anchor(p)}")}>{escape(p)}</a>'
                    f'<span class="d">{f"+{f.additions} −{f.deletions}" if f else ""}</span>'
                    f'<span class="meta">{"also " + escape(", ".join(shared[p])) if p in shared else ""}</span></div>')
    if c.stats:
        rows.append(f'      <div class="fl total"><span class="cap">total</span><span class="d">+{sum(f.additions for f in c.stats)} '
                    f'−{sum(f.deletions for f in c.stats)}</span><span></span></div>')
    return _section(f"Files changed · {len(rows) - 1 - bool(c.stats)}", '    <div class="card">\n' + "\n".join(rows) + "\n    </div>")


def _review(c: board_sources.Change, now: datetime, linker: Linker = Linker()) -> str:
    """REVIEW: open threads, then resolved, each newest first."""
    if not c.threads:
        return _section("Review", f'    <div class="card"><p>No review threads on {escape(c.ref)}.</p></div>')
    newest = sorted(c.threads, key=lambda t: t.at.timestamp() if t.at else 0, reverse=True)
    rows = []
    for t in sorted(newest, key=lambda t: t.resolved):
        when = f" · {_clock(t.at.astimezone(now.tzinfo), now)}" if t.at else ""
        t_url = external_url(t.url)
        link = f' · <a {href(t_url)}>thread</a>' if t_url else ""
        rows.append(f'      <div class="rv"><span class="mk{" passed" if t.resolved else ""}">{"Resolved" if t.resolved else "Open"}</span>'
                    f"<span><b>{escape(t.author)}</b>{when}{link}</span><div class=\"prose\">{markdown_blocks(drop_html_comments(t.body), linker.resolve)}</div></div>")
    resolved = sum(t.resolved for t in c.threads)
    return _section(f"Review · {len(c.threads) - resolved} open · {resolved} resolved",
                    '    <div class="card">\n' + "\n".join(rows) + "\n    </div>")


def _canonical_key(raw: str, home: Backlog | GitHubBacklog | None) -> str:
    if raw.startswith(("github:", "gitlab:")):
        return store_label(raw, home)
    return source_key(raw, home)


def _issue_linker(issue: board_sources.Issue | None, cache_key: str, cache_home: Backlog | GitHubBacklog | None,
                  cfg: Config, root: Path | None) -> Linker:
    """Resolve issue prose in the cached issue's namespace, never an unrelated current Backlog."""
    if issue is None:
        return Linker()
    refs = [issue_ref(raw, cache_home, any_host=True) for raw in (cache_key, issue.url) if raw]
    refs = [ref for ref in refs if ref is not None]
    def identity(ref):
        repo = ref.repo.casefold() if ref.host == "github.com" else ref.repo
        return ref.host, repo, ref.number

    if not refs or any(identity(ref) != identity(refs[0]) for ref in refs[1:]):
        return Linker()
    ref = refs[0]
    current = home_of(cfg.backlog) if cfg.backlog is not None else None
    local = root if current and current[0] == ref.host and (
        current[1].casefold() == ref.repo.casefold() if ref.host == "github.com" else current[1] == ref.repo
    ) else None
    if ref.host == "github.com":
        return Linker(GitHubBacklog(ref.repo), local)
    for home in (cache_home, cfg.backlog):
        if isinstance(home, Backlog) and home_of(home)[0] == ref.host:
            return Linker(home.sibling(ref.repo), local)
    if issue_ref(issue.url, None, any_host=True) == ref:
        return Linker(Backlog(f"https://{ref.host}", ref.repo), local)
    return Linker()


def _foreign_forge(raw_ref: str, backlog_cfg: Backlog | GitHubBacklog) -> bool:
    github_home = isinstance(backlog_cfg, GitHubBacklog)
    if raw_ref.startswith("github:"):
        return not github_home
    if raw_ref.startswith("gitlab:"):
        host, has_proj, _ = raw_ref[len("gitlab:"):].partition("/")
        return not (has_proj and not github_home and home_of(backlog_cfg)[0] == host)
    any_ref = issue_ref(raw_ref, backlog_cfg, any_host=True)
    if any_ref is None:
        return False
    own_ref = issue_ref(raw_ref, backlog_cfg)
    return own_ref is None or (own_ref.host == "github.com") != github_home


def _unread_reason(raw_refs: tuple[str, ...], display_ref: str, sources: board_sources.Sources,
                   cfg_given: Config | None, cfg: Config, now: datetime) -> str:
    if err := (sources.errors.get("kanban") or sources.errors.get("config")):
        return f"the forge read failed ({err})"
    if cfg.backlog is not None and raw_refs and all(_foreign_forge(r, cfg.backlog) for r in raw_refs):
        return "its forge does not match the configured Backlog"
    if cfg_given is not None and cfg_given.backlog is None and "kanban" not in sources.fetched:
        return "no Backlog is configured"
    if "kanban" in sources.fetched:
        when = _clock(sources.fetched["kanban"].astimezone(now.tzinfo), now)
        return f"{display_ref} was not in the forge cache read at {when}"
    return "the issue has not been read from the forge yet"


def _stale_note(panel: str, sources: board_sources.Sources, now: datetime) -> str:
    if not (err := sources.errors.get(panel)):
        return ""
    if panel in sources.fetched:
        when = _clock(sources.fetched[panel].astimezone(now.tzinfo), now)
        return f"Stale: cached at {when}; latest forge read failed ({err})."
    return f"Stale: latest forge read failed ({err})."


def _issue_section(issue: board_sources.Issue | None, linker: Linker, *,
                   unread: str = "", stale: str = "") -> str:
    if issue is None:
        why = unread or "the issue has not been read from the forge yet"
        return _section("Issue", f'    <div class="card unread"><p class="why">Issue body not read: {escape(why)}.</p></div>')
    body_text = drop_html_comments(issue.body)
    if stale:
        body = markdown_blocks(body_text, linker.resolve) if issue.body else "<p>No description in the issue.</p>"
        return _section("Issue", f'    <div class="card prose unread"><p class="meta why">{escape(stale)}</p>{body}</div>')
    if issue.body:
        return _section("Issue", f'<div class="card prose">{markdown_blocks(body_text, linker.resolve)}</div>')
    return ""


def _acceptance(issue: board_sources.Issue | None, linker: Linker = Linker(), *,
                unread: str = "", stale: str = "") -> str:
    """ACCEPTANCE: the bullets under the body's "Acceptance" heading, up to the next heading; `[x]` is met."""
    if issue is None:
        why = unread or "the issue has not been read from the forge yet"
        return _section("Acceptance",
                        f'    <div class="card unread"><p class="why">Acceptance criteria not read: {escape(why)}.</p></div>')
    # Remove complete comments before parsing, then collect each item's own paragraphs (not nested items).
    body = drop_html_comments(issue.body)
    tokens = MarkdownIt("commonmark", {"html": True}).parse(body)
    met, in_acceptance = [], False
    quote_depth = 0
    for i, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quote_depth += 1
            continue
        if token.type == "blockquote_close":
            quote_depth -= 1
            continue
        if quote_depth:
            continue
        if token.type == "heading_open":
            if in_acceptance:
                break
            heading = tokens[i + 1].content
            in_acceptance = bool(re.match(r"(?i)^acceptance\b", heading))
        elif in_acceptance and token.type == "list_item_open" and token.markup in ("-", "*", "+"):
            parts = []
            for child in tokens[i + 1:]:
                if child.type == "list_item_close" and child.level == token.level:
                    break
                if child.type == "inline" and child.level == token.level + 2:
                    parts.append(child.content)
            criterion = "\n".join(parts).strip()
            check = re.match(r"^\[([ xX])\](?:\s+|$)", criterion)
            if check:
                criterion = criterion[check.end():].strip()
            if criterion:
                met.append((bool(check and check[1] in ("x", "X")), criterion))
    card_cls = "card unread" if stale else "card"
    note = f'      <p class="meta why">{escape(stale)}</p>\n' if stale else ""
    if not met:
        body = f'    <div class="{card_cls}">\n{note}      <p>No acceptance criteria in the issue.</p>\n    </div>' if stale else (
            '    <div class="card"><p>No acceptance criteria in the issue.</p></div>')
        return _section("Acceptance", body)
    rows = "\n".join(f'      <div class="chk"><span class="mk{" passed" if ok else ""}">{"met" if ok else "not met"}</span>'
                     f"<span>{markdown_inline(what, linker.resolve)}</span></div>" for ok, what in met)
    return _section(f"Acceptance · {sum(ok for ok, _ in met)} of {len(met)} met", f'    <div class="{card_cls}">\n{note}{rows}\n    </div>')


def _decisions(db: PageData, key: str) -> str:
    """The issue's store decisions, newest first, each linked to its page, where it is answered (§6, #945)."""
    ids = addresses(db.decision_rows)
    found = [state_of(r) for r in reversed(db.decisions()) if r.issue_key == key]
    lines = "\n".join(f'      <p><a href="/decisions/{ids.get(d.id, d.id)}">{escape(d.prompt)}</a> {store_decision_badge(d)}</p>'
                      for d in found)
    return _section("Decisions", f'    <div class="card">\n{lines}\n    </div>') if found else ""


def _next(rows, change: board_sources.Change | None, changes, why_not: str, now: datetime,
          exhaustion=None) -> str:
    """WHAT HAPPENS NEXT: the Flow row's forecast stages and the change's place in its merge queue.

    An exhausted retry states the last failure and what unblocks it (#963); the engine will not re-dispatch.
    """
    if exhaustion is not None:
        lines = [f"Retries exhausted: {escape(exhaustion.reason)}."]
        if exhaustion.failure:
            lines.append(f"Last failure: {escape(exhaustion.failure)}.")
        lines.append(escape(exhaustion.unblock or EXHAUSTED_UNBLOCK))
    elif why_not:
        lines = [f"Nothing happens next: {why_not}."]
    else:
        lines = [f"{escape(g.category)} ~{_clock(g.start.astimezone(now.tzinfo), now)}–{_clock(g.end.astimezone(now.tzinfo), now)}"
                 for _, r in rows for g in r.segments if g.kind == "forecast"]
        lines = lines or ["No estimate: the task has no due time to forecast from."]
        if change and (q := _queue(change, changes)):
            lines.append(f"{escape(change.ref)} merge queue {q[0]} of {q[1]} into {escape(change.base)}, {escape(_after(q[2]))}")
    return _section("What happens next", '    <div class="card">\n' + "\n".join(f"      <p>{x}</p>" for x in lines) + "\n    </div>")


def render(number: int | str, trackers: list[tuple[date, str]], sources: board_sources.Sources, now: datetime,
           lanes: dict | None = None, cfg: Config | None = None, graph: Graph | None = None, pages: Path | None = None,
           root: Path | None = None, pending: int = 0, kanban: Kanban | None = None,
           pending_refs: Container[str] = frozenset(), navigation: dict | None = None, db: PageData | None = None,
           engine_status: EngineStatus | None = None) -> str | None:
    """The page, or None when no task in `trackers` or the store names issue `number`. `cfg` resolves a task's due (the
    board's deadlines); `graph`, `pages` and `root` draw Architecture as the decision page draws its module graph."""
    home = cfg.backlog if cfg else None
    same_home = (sources.home is not None and home is not None and type(sources.home) is type(home)
                 and board_sources.backlog.home_of(sources.home) == board_sources.backlog.home_of(home))
    sources = board_sources.for_home(sources, home)
    identity = str(number)
    full_key = identity if "#" in identity else None
    numeric = issue_number(identity) if full_key else int(identity)
    ref = f"#{numeric}"
    cfg_given = cfg
    cfg = cfg or Config("", "", "", getattr(now.tzinfo, "key", "UTC"), (), None, None)
    cache_home = sources.home
    home = sources.home if sources.home is not None else cfg.backlog
    if sources.home is None and home is not None:
        sources = replace(sources, home=home)
    names = lambda t: {k.strip().casefold() for k in (t.name, t.item) if k.strip()}  # noqa: E731
    known = [t for _, text in trackers for t in parse_tracker(text).tasks]
    mine = tracker_issue_tasks(trackers, numeric, home, full_key)
    if mine is None and db is None:
        return None
    mine = mine or []
    store_rows = None
    item = None
    if db is not None:
        matches = [i for i in db.items() if (i.issue_key == full_key if full_key else issue_number(i.issue_key) == numeric)]
        item = matches[0] if len(matches) == 1 else None
        if full_key and item is None:
            return None
        if not full_key and len(matches) > 1:
            return None
        if item is not None:
            display_ref = store_label(item.issue_key, home) if (home is not None or full_key) else ref
            raw_refs = (item.issue_key,)
            lookup = lookup_keys(raw_refs, home)
            cache_key = next((k for k in lookup if k in sources.issues), "")
            issue = sources.issues.get(cache_key)
            title = item.title or (issue.title if issue else display_ref)
            task = Task(item=title, owner="",
                        state="waiting" if item.human_review else "open",
                        since=now.date().isoformat(), due="", checklist="",
                        name=title, lane=item.workflow or "build",
                        stage=item.stage, issue=item.issue_key)
            known = [task]
            mine = [task]
            store_rows = [(s, r) for s, r in flow_chart.build_from_store(db, now, include_hold_bars=True)
                          if r.href == issue_href(item.issue_key)]
    if not mine:
        return None
    if item is None:
        raw_refs = tuple(dict.fromkeys(t.issue.strip() for t in mine if t.issue.strip() and not t.workflow))
        if full_key:
            lookup = lookup_keys((full_key,), home)
            display_ref = store_label(full_key, home)
        else:
            resolved = list(dict.fromkeys(_canonical_key(r, home) for r in raw_refs))
            if (len(resolved) > 1 if home is not None else len({k for k in resolved if k != ref}) > 1):
                return None
            lookup = lookup_keys(raw_refs, home)
            display_ref = resolved[0] if len(resolved) == 1 and not resolved[0].startswith("http") else ref
        cache_key = next((k for k in lookup if k in sources.issues), "")
        issue = sources.issues.get(cache_key)
    issue_keys = set(lookup)
    keys = set().union(*map(names, mine))
    # Resolve each day's aliases before building Flow: names may be reused by another issue later.
    log = []
    selected = {source_key(t.issue, sources.home) for t in mine if t.issue.strip()}
    prior_aliases = {}
    for day, text in trackers:
        day_tasks = parse_tracker(text).tasks
        aliases = {}
        for t in day_tasks:
            identity_key = source_key(t.issue, sources.home) if t.issue.strip() else ""
            for alias in names(t):
                aliases.setdefault(alias, set()).add(identity_key)
        for move in tracker_log.moves(text, day, now.tzinfo):
            alias = move.name.strip().casefold()
            identities = aliases.get(alias, prior_aliases.get(alias, set()))
            if identities and identities <= selected:
                log.append(move)
        prior_aliases.update(aliases)
    log.sort(key=lambda m: m.at)
    # A forecast runs to the task's due, as the board's Flow chart feeds estimate.task_end's due.
    linker = Linker(cfg.backlog, root)
    issue_linker = _issue_linker(issue, cache_key, cache_home, cfg, root)
    today_tasks = parse_tracker(trackers[-1][1]).tasks if trackers else known
    ends = {t.item: end for t in today_tasks
            if t.kind != "done" and (end := _resolve_due(t.due, cfg, now.date()))}
    # (#200, #220) forge_ends is shared with the board, so both end a row at the same time
    # #228: held as the board holds it, from today's tasks less the standing ones, so both name the same cause.
    today = [t for t in today_tasks if not t.standing]
    holds = hold_causes(today, kanban, sources, lanes or {}, pending_refs)
    held = held_names(holds)
    rows = store_rows if store_rows is not None else [
        (s, r) for s, r in flow_chart.build(log, mine, lanes or {}, held, ends, now,
                                            ended=forge_ends(mine, sources, now.tzinfo), home=sources.home)
        if r.ref and r.ref in selected
    ]
    log = [m for m in log if m.name.strip().casefold() in keys]
    latest = {k: t for t in mine for k in names(t)}  # later selected rows win
    exhausted = db.exhaustion(item.issue_key) if item is not None and db is not None else None
    if exhausted is not None:
        status = EXHAUSTED_CHIP
    elif any(t in holds for t in mine):
        status = "needs input"
    else:
        status = rows[0][0] if rows else mine[-1].kind
    latest_mine = list({(t.name.strip() or t.item.strip()).casefold(): t for t in mine}.values())
    header_live = (any(s.issue_key == item.issue_key and s.ended_at is None for s in db.segments())
                   if item is not None and db is not None else any(t.kind == "running" for t in latest_mine))
    current_tasks = (latest_mine if item is not None else
                     list({(t.name.strip() or t.item.strip()).casefold(): t for t in known}.values()))
    header_live = header_live or bool(source_worker_tasks(sources, current_tasks).intersection(latest_mine))
    name = rows[0][1].name if rows else (issue.title if issue else mine[0].label)

    order = {"open": 0, "merged": 1}
    changes = sorted(
        (c for c in sources.changes.values()
         if issue_keys & (change_issue_keys(c) | {_canonical_key(r, home) for r in change_issue_keys(c) if r})),
        key=lambda c: order.get(c.state, 2),
    )
    every = list(sources.changes.values())
    ref_html = escape(display_ref)
    cards = "".join(_change(c, every, now) for c in changes) or _section("Change", f'    <div class="card"><p>No change names {ref_html} yet.</p></div>')
    change = changes[0] if changes else None
    current = {t.name.strip().casefold(): t for t in mine}.values()
    why_not = ("the issue is closed" if (item and item.forge_state == "closed") or (issue and issue.state == "closed") else
               "the task is done" if status == "merged" or all(t.kind == "done" for t in current) else "")
    source_url = f"{issue_href(identity)}/source"
    ahead = (_next(rows, change, every, why_not, now, exhaustion=exhausted)
             + (_files(change, every, source_url) if change and _paths(change) else ""))
    ahead += _graph(change, graph, pages, root, "Architecture", f"No change names {display_ref} to graph.")

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
    steps = "\n".join(f'      <div class="step"><span class="when">{_step_when(m.at, now)}</span><span class="dot" data-k="{escape(m.stage)}">'
                      f"</span><span>{what.get(m) or markdown_inline(m.line.split(' ', 2)[2], linker.resolve)}</span></div>" for m in log)
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
    if item is not None and db is not None:
        steps_list = []
        lines, spans = [], []
        for seg in db.segments():
            if seg.issue_key != item.issue_key:
                continue
            start = parse_stamp(seg.started_at, now)
            stop = parse_stamp(seg.ended_at, now) if seg.ended_at else now
            who = seg.runner or f"{seg.stage}-{seg.attempt}"
            took = max(timedelta(), stop - start)
            spans.append((start, max(start, stop)))
            steps_list.append((start, f'{escape(seg.stage)} started · {escape(who)}'))
            if seg.ended_at:
                steps_list.append((stop, f'{escape(seg.stage)} {escape(seg.outcome or "ended")} · {escape(who)}'))
            run_status = ("running" if seg.ended_at is None else
                          "failed to start" if seg.outcome == "failed" and seg.started_at == seg.ended_at else
                          seg.outcome or "ended")
            lines.append((took, f'<div class="wk"><b>{escape(who)}</b><span>{escape(seg.runtime)} '
                               f'{escape(seg.model)} · {escape(seg.stage)} · attempt {seg.attempt}</span>'
                               f'<span>{escape(run_status)} · {_dur(took)}</span></div>'))
        steps_list.extend((parse_stamp(entry.at, now), escape(entry.message))
                          for entry in db.log(item.issue_key))
        steps = "\n".join(f'<div class="step"><span class="when">{_step_when(at, now)}</span>'
                          f'<span class="dot"></span><span>{text}</span></div>'
                          for at, text in sorted(steps_list, key=lambda step: step[0]))
        end = max((e for _, e in spans), default=now)
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

    issue_url = external_url(issue.url) if issue else ""
    issue_label = f"{ref_html} {escape(issue.title if issue else name)}"
    refs = [("issue", f"<a {href(issue_url)}>{issue_label}</a>" if issue_url else issue_label,
             issue.state if issue else "")]
    if item is not None and db is not None:
        resume = db.resume(item.issue_key)
        if link := latest_thread_link(resume.get("runs", {}) if isinstance(resume, dict) else {}):
            refs.append(("worker", f'<a {href(link)}>T3 thread</a>', ""))
    for c in changes:
        c_url = external_url(c.url)
        c_label = f"{escape(c.ref)} {escape(c.title)}"
        refs.append(("MR" if c.ref.startswith("!") else "PR",
                     f"<a {href(c_url)}>{c_label}</a>" if c_url else c_label,
                     " · ".join(filter(None, (c.state, c.pipeline)))))
    seen_branches: set[str] = set()
    for c in changes:
        if not c.branch or c.branch in seen_branches:
            continue
        seen_branches.add(c.branch)
        b_url = external_url(c.branch_url)
        b_html = (f'<a class="mono" {href(b_url)}>{escape(c.branch)}</a>' if b_url
                  else f'<span class="mono">{escape(c.branch)}</span>')
        refs.append(("branch", b_html, f"@ {c.head[:7]}" if c.head else ""))
    if not seen_branches and item is not None and db is not None:
        resume = db.resume(item.issue_key)
        work_branch = resume.get("work_branch") if isinstance(resume, dict) else ""
        if isinstance(work_branch, str) and work_branch.strip():
            refs.append(("branch", f'<span class="mono">{escape(work_branch.strip())}</span>', "worktree"))
    paths = _paths(change) if change else ()
    source_st = f"{len(paths)} file{'s' * (len(paths) != 1)}" if paths else (change.ref if change else "")
    refs.append(("source", f'<a {href(source_url)}>source &amp; diff</a>', source_st))
    refs_html = "\n".join(f'      <span class="cap">{cap}</span><span>{what}</span><span class="st">{escape(st)}</span>' for cap, what, st in refs)

    health = engine_health(engine_status, now, cfg.zone) if engine_status is not None else None
    lane = next((t.lane.strip() for t in reversed(mine) if t.lane.strip()), "")
    eyebrow = " · ".join(["Task",
                          f'<a class="ref" data-k="issue" {href(issue_url)}>{ref_html}</a>' if issue_url else ref_html,
                          *([f"lane {escape(lane)}"] if lane else []),
                          *(tuple(escape(m) for m in health.meta) if health is not None else ()),
                          f'<a {href(source_url)}>source</a>',
                          '<a href="/">board</a>'])  # (#215) no render clock
    warn_html = (f'  <p class="engine-warn" role="alert">{escape(health.warning)}</p>\n'
                 if health is not None and health.warning else "")
    flow = flow_chart.section(rows, now)
    unread = _unread_reason(raw_refs, display_ref, sources, cfg_given, cfg, now) if issue is None else ""
    stale_issue = _stale_note("kanban", sources, now) if issue is not None else ""

    warnings = []
    if cfg and cfg.backlog:
        for panel in ("kanban", "merge requests"):
            if panel in sources.errors:
                state = "stale" if same_home and panel in sources.fetched else "unavailable"
                warnings.append(f"{panel}: {state} — {sources.errors[panel]}")
            elif not same_home or panel not in sources.fetched:
                warnings.append(f"{panel}: unavailable — not fetched yet")
    source_warning = (f'<p class="card">{escape("; ".join(warnings))}</p>\n' if warnings else "")
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{ref_html} {escape(name)}</title>\n{HEAD}<style>\n{flow_chart.css() if flow else ''}</style>\n{CSS}</head>\n"
            f'<body>\n<main class="issue">\n{tab_bar("Board", pending, True, **(navigation or {}))}  <header class="top">\n    <div>\n      <span class="eyebrow">{eyebrow}</span>\n'
            f"      <h1>{escape(name)}</h1>\n    </div>\n"
            f'    <div><span class="chip {escape(status.replace(" ", "-"))}">{escape(status.upper())}</span>'
            + (' <span class="chip running">RUNNING</span>' if header_live and status != "running" else '') + '</div>\n  </header>\n\n'
            + warn_html + source_warning
            + _issue_section(issue, issue_linker, unread=unread, stale=stale_issue)
            + cards + (f"  <section>\n{flow}{worked_line}  </section>\n" if flow else "") + ahead
            + '  <div class="cols">\n  <div class="col">\n'
            + _section("How it got here", f'    <div class="card timeline">\n{steps}\n    </div>' if steps else '    <div class="card"><p>No Log lines yet.</p></div>')
            + (_review(change, now, linker) if change else "")
            + '  </div>\n  <div class="col">\n' + _acceptance(issue, issue_linker, unread=unread, stale=stale_issue)
            + (_decisions(db, item.issue_key) if item is not None and db is not None else "")
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


def write(root: Path, pages_dir: Path, number: int | str, *, db: PageData | None = None, navigation: dict | None = None) -> Path | None:
    """Render `issue-<number>.html`; with no page, remove the file and return None. The page server calls this in
    process."""
    cfg, now, trackers, settings = inputs(root)
    ctx = context(root, now.date().isoformat())
    pending = entries(pages_dir, ctx) if ctx else []
    page = render(number, trackers, board_sources.load(pages_dir), now, settings.lanes, cfg, settings.graph, pages_dir, root,
                  sum(d is not None for _, d, _, _ in pending), settings.kanban, held_refs(pending, ctx),
                  navigation, db=db, engine_status=read_engine_status(root))
    return put(pages_dir / issue_page_name(number), page)

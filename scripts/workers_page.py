#!/usr/bin/env python3
"""The Workers page, `/workers` (#195, #235): header tiles (`NEEDS INPUT`, `RUNNING`, `SLOTS FREE`); RUNNING, a row per
worker with a task (sessions with no task are omitted), leading with a state chip (`RUNNING`, `LOOPING`) and the worker
name over `runtime model · kind`, the stage as a chip after the task name, and last activity and tokens from its Claude
session log; and ENDED TODAY, a row per one-shot launcher end line in today's tracker with a state chip (`DONE`,
`CRASHED`), a one-line summary, and a link or `re-dispatch`. pages.py renders it into `workers.html` on every request.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape
from pathlib import Path

from decision_actions import states
import board_sources
import panels
import session_logs
from clock import parse_stamp
from task_forge import PageData, workers_need_input
from decision_page import HEAD, context, pending_count, write_if_changed
from fragment import tab_bar, issue_href
from md import section as _section
from tracker import TRAILING_NUMBER, launcher, parse_tracker
from tracker_log import COMPLETED, ENDED, RELAUNCH, STAGE, STARTED
from workspace import parse_coordinator, worktrees_dir
from settings import load as load_settings

USAGE = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
CSS = """<style>
.boxes{display:flex;flex-wrap:wrap;gap:8px}
.box{background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:4px 12px;display:flex;flex-direction:column;align-items:flex-end}
.box b{font:700 18px ui-monospace,monospace;font-variant-numeric:tabular-nums}
.box.hot{border-color:var(--dl);color:var(--dl)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:400;padding:4px 8px}
td{padding:6px 8px;border-top:1px dotted var(--line);vertical-align:baseline}
.lead{display:inline-flex;align-items:baseline;gap:8px}
.meta{color:var(--muted);font-size:12px}
.chip{border:1px solid var(--line);color:var(--fg)}
.chip.running{color:var(--surface);background:var(--stage-implement);border-color:var(--stage-implement)}
.chip.looping,.chip.crashed{color:var(--dl);border-color:var(--dl)}
.chip.done{color:var(--surface);background:var(--stage-review);border-color:var(--stage-review)}
.chip.stage{font-weight:500;color:var(--muted)}
</style>
"""


def activity(tree: Path, *, account: str = "") -> tuple[datetime | None, int]:
    """(last timestamp, tokens) from the selected Claude account, counting each message id once."""
    last, usage = None, {}
    try:
        for log, n, event in session_logs.activity_events(account, tree):
            try:
                at = datetime.fromisoformat(event["timestamp"])
                last = at if last is None or at > last else last
                message = event.get("message") or {}
                usage[message.get("id") or (log, n)] = message.get("usage") or {}
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    except (ValueError, OSError):
        pass
    return last, sum(v for u in usage.values() if isinstance(u, dict) for k in USAGE if isinstance(v := u.get(k), int))


def _task(t, fallback: str) -> str:
    """The task's name, linked to its page when its issue cell has a number."""
    name = escape(t.name.strip() if t and t.name.strip() else fallback)
    if t and ":" in t.issue and "#" in t.issue and not t.issue.strip().startswith(("http://", "https://")):
        raw = t.issue.strip()
        num = raw.rsplit("#", 1)[1]
        return f'<a class="ref" data-k="issue" href="{escape(issue_href(raw))}">#{escape(num)}</a> {name}'
    m = TRAILING_NUMBER.search(t.issue) if t else None
    return f'<a class="ref" data-k="issue" href="/issues/{m[1]}">#{m[1]}</a> {name}' if m else name


def _task_stage(t, fallback: str) -> str:
    """The task's ref and name, followed by its stage as a chip when set."""
    base = _task(t, fallback)
    stage = escape(t.stage.strip()) if t and t.stage.strip() else ""
    return f'{base} <span class="chip stage">{stage}</span>' if stage else base


def _worker_lead(w: board_sources.Worker, looping: bool) -> str:
    """State chip (`RUNNING`, `LOOPING`) and the worker name stacked over `runtime model · kind`."""
    state = "LOOPING" if looping else "RUNNING"
    sub = " · ".join(x for x in (" ".join(x for x in (w.runtime, w.model) if x), w.kind) if x)
    return (f'<span class="lead"><span class="chip {state.lower()}">{state}</span>'
            f'<span class="stack"><b>{escape(w.name)}</b><span class="meta">{escape(sub)}</span></span></span>')


def _table(heads: tuple[str, ...], rows: list[list[str]]) -> str:
    head = "".join(f'<th class="cap">{h}</th>' for h in heads)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>\n" for r in rows)
    return f'    <div class="card scroll"><table>\n<tr>{head}</tr>\n{body}</table></div>'


def _tracker_ends(tracker_text: str, row_of) -> tuple[dict[str, int], list[list[str]]]:
    """From the tracker's Log: fix-loop counts by task, and ENDED TODAY rows newest first."""
    task_of, fix_counts, last_stage, ended = {}, {}, {}, []
    for line in map(str.strip, _section(tracker_text, "## Log")):
        if m := STAGE.match(line):
            t, name = row_of(m[3].strip())
            key = (t.name.strip() if t and t.name.strip() else name).casefold()
            stage = m[4].strip().casefold()
            if stage == "fix" and last_stage.get(key) != "fix":
                fix_counts[key] = fix_counts.get(key, 0) + 1
            last_stage[key] = stage
        elif m := STARTED.match(line):
            task_of[m[3]] = m[4]
        elif m := ENDED.match(line) or RELAUNCH.match(line):
            t, name = row_of(task_of.get(m[3], ""))
            done = m[4] == COMPLETED
            state = "DONE" if done else "CRASHED"
            summary = (line[m.end():].removeprefix(" — ").strip() if m.re is ENDED
                       else line[m.end():].partition(" — ")[2].strip())
            ref = TRAILING_NUMBER.search(t.issue) if t else None
            action = ((f'<a class="ref" data-k="issue" href="/issues/{ref[1]}">#{ref[1]}</a>' if ref else escape(name))
                      if done else '<span class="chip redispatch">re-dispatch</span>')
            ended.append([f'<span class="chip {state.lower()}">{state}</span> <b>{escape(m[3])}</b>',
                          f"{int(m[1]):02d}:{m[2]}", _task(t, name), escape(summary), action])
    ended.reverse()  # newest first
    return fix_counts, ended


def _running_row(lead: str, task: str, started: datetime, now: datetime, tree: Path | None, account: str) -> list[str]:
    last, tokens = activity(tree, account=account) if tree else (None, 0)
    return [lead, task, panels.hm(started, now) if started else "",
            f"{last.astimezone(now.tzinfo):%H:%M}" if last else "", f"{tokens:,}" if last else ""]


def _session_rows(sources: board_sources.Sources, row_of, fix_counts: dict[str, int], by_name: dict, now: datetime,
                  trees: Path, account: str) -> tuple[list[list[str]], set[str]]:
    """RUNNING rows from the board's worker sources, and the names they cover."""
    running: list[list[str]] = []
    seen_running: set[str] = set()
    try:
        session_logs.retain_activity(account, [trees / w.tree for w in sources.workers if w.tree])
    except (ValueError, OSError):
        pass
    for w in sources.workers:
        if w.kind == "session" and not w.task.strip():
            continue
        seen_running.add(w.name)
        t = by_name.get(w.task.strip().casefold()) or row_of(w.task)[0]
        key = (t.name.strip() if t and t.name.strip() else w.task.strip()).casefold()
        is_looping = fix_counts.get(key, 0) >= 2 or bool(t and t.stage.strip().casefold() == "looping")
        running.append(_running_row(_worker_lead(w, is_looping), _task_stage(t, w.task), w.started, now,
                                    trees / w.tree if w.tree else None, account))
    return running, seen_running


def _store_rows(db: PageData, seen_running: set[str], now: datetime, trees: Path,
                account: str) -> tuple[list[list[str]], list[list[str]], int]:
    """RUNNING and ENDED TODAY rows from the store's segments, and the running one-shot count."""
    running: list[list[str]] = []
    ended: list[list[str]] = []
    store_shots = 0
    all_segs = db.segments()
    fix_by_issue: dict[str, int] = {}
    for s in all_segs:
        if s.stage.strip().casefold() == "fix":
            fix_by_issue[s.issue_key] = fix_by_issue.get(s.issue_key, 0) + 1
    for seg in all_segs:
        item = db.item(seg.issue_key)
        runner = getattr(seg, "runner", "") or f"{seg.stage}-{seg.attempt}"
        title = item.title if item and item.title else seg.issue_key
        num = seg.issue_key.rsplit("#", 1)[1] if "#" in seg.issue_key else seg.issue_key
        task_link = f'<a class="ref" data-k="issue" href="{escape(issue_href(seg.issue_key))}">#{escape(num)}</a> {escape(title)}'
        if seg.ended_at is None:
            if runner in seen_running:
                continue
            seen_running.add(runner)
            store_shots += 1
            started_dt = parse_stamp(seg.started_at, now)
            w_obj = board_sources.Worker(runner, "one-shot", seg.runtime, seg.model, title, runner, started_dt, None)
            is_looping = fix_by_issue.get(seg.issue_key, 0) >= 2
            stage_chip = f' <span class="chip stage">{escape(seg.stage)}</span>' if seg.stage else ""
            tree = trees / runner
            running.append(_running_row(_worker_lead(w_obj, is_looping), f"{task_link}{stage_chip}", started_dt, now,
                                        tree if tree.exists() else None, account))
        else:
            ended_dt = parse_stamp(seg.ended_at, now)
            if ended_dt.date() != now.date():
                continue
            reports = db.reports(seg.issue_key, stage=seg.stage, attempt=seg.attempt)
            summary = reports[-1].summary if reports else (seg.outcome or "")
            done = seg.outcome == "done"
            state = "DONE" if done else "CRASHED"
            action = (f'<a class="ref" data-k="issue" href="{escape(issue_href(seg.issue_key))}">#{escape(num)}</a>'
                      if done else '<span class="chip redispatch">re-dispatch</span>')
            ended.append([f'<span class="chip {state.lower()}">{state}</span> <b>{escape(runner)}</b>',
                          f"{ended_dt:%H:%M}", task_link, escape(summary), action])
    return running, ended, store_shots


def render(tracker_text: str, sources: board_sources.Sources, now: datetime, trees: Path, cap: int | None,
           pending: int = 0, navigation: dict | None = None, db: PageData | None = None, *, claude_account: str = "") -> str:
    """The page. `trees` is where one-shot trees sit; `cap` is `[workers] max_concurrency`."""
    tasks = parse_tracker(tracker_text).tasks
    by_name = {t.name.strip().casefold(): t for t in tasks if t.name.strip()}
    row_of = launcher(tasks)
    fix_counts, ended = _tracker_ends(tracker_text, row_of)
    running, seen_running = _session_rows(sources, row_of, fix_counts, by_name, now, trees, claude_account)
    store_shots = 0
    if db is not None:
        store_running, store_ended, store_shots = _store_rows(db, seen_running, now, trees, claude_account)
        running += store_running
        ended += store_ended
    # The same count the tab badge shows, including when the caller passed a navigation dict (#898).
    need = workers_need_input(tracker_text, None if db is None else db.items(), () if db is None else db.segments())
    navigation = {**(navigation or {}), "workers": need}
    shots = sum(w.kind == "one-shot" for w in sources.workers) + store_shots
    boxes = [("NEEDS INPUT", str(need), " hot" if need else ""), ("RUNNING", str(len(running)), "")]
    if cap:
        boxes.append(("SLOTS FREE", f"{max(cap - shots, 0)} of {cap}", ""))
    boxes_html = "".join(f'<span class="box{hot}"><b>{v}</b><span class="cap">{k}</span></span>' for k, v, hot in boxes)
    eyebrow = " · ".join([*(f"{escape(k)}: {escape(why)}" for k, why in sources.errors.items()),  # (#215) no render clock
                          '<a href="/">board</a>'])
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>Workers</title>\n{HEAD}{CSS}</head>\n<body>\n<main>\n{tab_bar('Workers', pending, **navigation)}"
            f'  <header class="top">\n    <div>\n      <h1>Workers</h1>\n      <span class="eyebrow">{eyebrow}</span>\n    </div>\n'
            f'    <div class="boxes">{boxes_html}</div>\n  </header>\n'
            f'  <section>\n    <h2>Running · {len(running)}</h2>\n'
            + _table(("worker", "task", "running", "last activity", "tokens"), running)
            + f'\n  </section>\n  <section>\n    <h2>Ended today · {len(ended)}</h2>\n'
            + _table(("worker", "at", "task", "summary", "action"), ended)
            + "\n  </section>\n</main>\n</body>\n</html>\n")


def write(root: Path, pages_dir: Path, *, db: PageData | None = None, navigation: dict | None = None) -> Path:
    """Render `workers.html`; the page server calls this in process on every request."""
    claude_md = (root / "CLAUDE.md").read_text()
    cfg = parse_coordinator(claude_md, today=date.today())
    now = datetime.now(cfg.zone)  # not cut to the second: a time running floors, as panels.hm counts
    tracker = root / cfg.tracker_path(now.date().isoformat())
    ctx = context(root, now.date().isoformat())
    store_pending = sum(1 for d in states(db.decisions()) if d.status in ("open", "blocked")) if db is not None else 0
    workers = load_settings(root, cfg.settings_path).workers
    page = render(tracker.read_text() if tracker.is_file() else "", board_sources.load(pages_dir), now,
                  root / worktrees_dir(claude_md), workers.max_concurrency,
                  pending_count(pages_dir, ctx) + store_pending, navigation, db=db, claude_account=workers.claude_as_profile)
    out = pages_dir / "workers.html"
    write_if_changed(out, page)
    return out

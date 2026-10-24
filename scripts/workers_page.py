#!/usr/bin/env python3
"""The Workers page, `/workers`: state and stage chips for workers with tasks, with last activity and tokens from
their Claude session logs; and ENDED TODAY, outcomes and summaries from today's one-shot launcher end lines.
pages.py renders it into `workers.html` on every request.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from html import escape
from pathlib import Path

import board_sources
import panels
from decision_page import HEAD, context, pending_count, write_if_changed
from fragment import tab_bar
from md import section as _section
from tracker import TRAILING_NUMBER, launcher, parse_tracker
from tracker_log import COMPLETED, CRASH, ENDED, HUMAN_REVIEW, RELAUNCH, STARTED, reason
from workspace import parse_coordinator, worktrees_dir
from settings import load as load_settings

# Human review becomes CRASHED only when the launcher recorded a failed run.
HOW = {COMPLETED: "DONE", HUMAN_REVIEW: "REVIEW", "relaunch": "RELAUNCH"}
USAGE = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
CSS = """<style>
.boxes{display:flex;flex-wrap:wrap;gap:8px}
.box{background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:4px 12px;display:flex;flex-direction:column;align-items:flex-end}
.box b{font:700 18px ui-monospace,monospace;font-variant-numeric:tabular-nums}
.box.hot{border-color:var(--dl);color:var(--dl)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:400;padding:4px 8px}
td{padding:6px 8px;border-top:1px dotted var(--line);vertical-align:baseline}
.worker-detail{display:block;margin-top:3px;color:var(--muted);font-size:12px}
.worker-name{font-weight:700}
td .chip{display:inline-block;border:1px solid var(--line);margin-right:6px}
td .chip.running{color:var(--inflight);background:var(--inflight-soft);border-color:var(--inflight)}
td .chip.looping{color:var(--designed);background:var(--designed-soft);border-color:var(--designed)}
td .chip.done{color:var(--deployed);border-color:var(--deployed)}
td .chip.crashed,td .chip.review{color:var(--dl);border-color:var(--dl)}
td .chip.relaunch{color:var(--inflight);border-color:var(--inflight)}
td .chip.stage{color:var(--designed);background:var(--designed-soft);border-color:var(--designed);margin:0 0 0 6px}
.summary{display:block;max-width:40ch;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
</style>
"""


def activity(tree: Path) -> tuple[datetime | None, int]:
    """(last line's timestamp, tokens) over the Claude transcripts of a session run in `tree`; (None, 0) with none.
    A message split over lines repeats its usage, so each message id counts once."""
    last, usage = None, {}
    for log in (Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(tree.resolve()))).glob("*.jsonl"):
        with log.open(errors="replace") as f:
            for n, line in enumerate(f):
                try:
                    d = json.loads(line)
                    at = datetime.fromisoformat(d["timestamp"])
                    last = at if last is None or at > last else last
                    message = d.get("message") or {}
                    usage[message.get("id") or (log, n)] = message.get("usage") or {}
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    return last, sum(v for u in usage.values() if isinstance(u, dict) for k in USAGE if isinstance(v := u.get(k), int))


def _task(t, fallback: str, link_label: str = "") -> str:
    """The task's name, linked to its page when its issue cell has a number."""
    name = escape(t.name.strip() if t and t.name.strip() else fallback)
    m = TRAILING_NUMBER.search(t.issue) if t else None
    return f'<a class="ref" data-k="issue" href="/issues/{m[1]}">{escape(link_label) if link_label else "#" + m[1]}</a> {name}' if m else name


def _chip(label: str) -> str:
    return f'<span class="chip {label.lower()}">{label}</span>'


def _table(heads: tuple[str, ...], rows: list[list[str]]) -> str:
    head = "".join(f'<th class="cap">{h}</th>' for h in heads)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>\n" for r in rows)
    return f'    <div class="card scroll"><table>\n<tr>{head}</tr>\n{body}</table></div>'


def render(tracker_text: str, sources: board_sources.Sources, now: datetime, trees: Path, cap: int | None,
           pending: int = 0) -> str:
    """The page. `trees` is where one-shot trees sit; `cap` is `[workers] max_concurrency`."""
    tasks = parse_tracker(tracker_text).tasks
    by_name = {t.name.strip().casefold(): t for t in tasks if t.name.strip()}
    workers = [w for w in sources.workers if w.kind != "session" or w.task.strip()]
    running = []
    for w in workers:
        t = by_name.get(w.task.strip().casefold())
        last, tokens = activity(trees / w.tree) if w.tree else (None, 0)
        detail = " · ".join(x for x in (" ".join(filter(None, (w.runtime, w.model))), w.kind) if x)
        worker = (f'{_chip("LOOPING" if w.kind == "session" else "RUNNING")} '
                  f'<span class="worker-name">{escape(w.name)}</span>'
                  f'<span class="worker-detail">{escape(detail)}</span>')
        stage = f' <span class="chip stage">{escape(t.stage.strip())}</span>' if t and t.stage.strip() else ""
        running.append([worker, _task(t, w.task) + stage, panels.hm(w.started, now) if w.started else "",
                        f"{last.astimezone(now.tzinfo):%H:%M}" if last else "", f"{tokens:,}" if last else ""])
    row_of, task_of, ended, need = launcher(tasks), {}, [], 0
    for line in map(str.strip, _section(tracker_text, "## Log")):
        if m := STARTED.match(line):
            task_of[m[3]] = m[4]
        elif m := ENDED.match(line) or RELAUNCH.match(line):
            t, name = row_of(task_of.get(m[3], ""))
            summary = reason(line)
            state = "CRASHED" if m[4] == HUMAN_REVIEW and CRASH.match(summary) else HOW[m[4]]
            need += m[4] == HUMAN_REVIEW
            ended.append([f"{int(m[1]):02d}:{m[2]}", _chip(state), escape(m[3]),
                          f'<span class="summary" title="{escape(summary)}">{escape(summary)}</span>',
                          _task(t, name, "" if state == "DONE" else "re-dispatch")])
    ended.reverse()  # newest first
    shots = sum(w.kind == "one-shot" for w in workers)
    boxes = [("NEEDS INPUT", str(need), " hot" if need else ""), ("RUNNING", str(len(running)), "")]
    if cap:
        boxes.append(("SLOTS FREE", f"{max(cap - shots, 0)} of {cap}", ""))
    boxes_html = "".join(f'<span class="box{hot}"><b>{v}</b><span class="cap">{k}</span></span>' for k, v, hot in boxes)
    eyebrow = " · ".join([*(f"{escape(k)}: {escape(why)}" for k, why in sources.errors.items()),  # (#215) no render clock
                          '<a href="/">board</a>'])
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>Workers</title>\n{HEAD}{CSS}</head>\n<body>\n<main>\n{tab_bar('Workers', pending)}"
            f'  <header class="top">\n    <div>\n      <h1>Workers</h1>\n      <span class="eyebrow">{eyebrow}</span>\n    </div>\n'
            f'    <div class="boxes">{boxes_html}</div>\n  </header>\n'
            f'  <section>\n    <h2>Running · {len(running)}</h2>\n'
            + _table(("worker", "task", "running", "last activity", "tokens"), running)
            + f'\n  </section>\n  <section>\n    <h2>Ended today · {len(ended)}</h2>\n'
            + _table(("at", "state", "worker", "summary", "task"), ended)
            + "\n  </section>\n</main>\n</body>\n</html>\n")


def write(root: Path, pages_dir: Path) -> Path:
    """Render `workers.html`; the page server calls this in process on every request."""
    claude_md = (root / "CLAUDE.md").read_text()
    cfg = parse_coordinator(claude_md, today=date.today())
    now = datetime.now(cfg.zone)  # not cut to the second: a time running floors, as panels.hm counts
    tracker = root / cfg.tracker_path(now.date().isoformat())
    page = render(tracker.read_text() if tracker.is_file() else "", board_sources.load(pages_dir), now,
                  root / worktrees_dir(claude_md), load_settings(root, cfg.settings_path).workers.max_concurrency,
                  pending_count(pages_dir, context(root, now.date().isoformat())))
    out = pages_dir / "workers.html"
    write_if_changed(out, page)
    return out

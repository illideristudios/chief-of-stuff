"""The board's Flow charts: each task's stage history, read from the tracker's `stage:` Log lines, drawn by gantt.py.

`chief-of-stuff log --stage` writes `- HH:MM stage: <name> → <stage>`. A segment runs from each move to the
next and the last to now; a lane's last stage ends the row. A held task is hatched from now through the
window, and an open task with an estimated end draws its remaining stages ahead as forecast. An open task at
its lane's first stage with no move yet is queued: forecast only, after the running forecasts, in worker slots.
Rows that share a tracker issue (a review loop's build/Review/Fix/Verify passes) merge into one row.
Until a task's first `stage:` move, it moves on the one-shot launcher's lines: started → implement, completed → pr,
HUMAN REVIEW NEEDED → review. A relaunch request ends its worker's bar without starting another stage.
A done task's last segment ends at its done time, not now.
"""

from __future__ import annotations

import heapq
from dataclasses import replace
from datetime import datetime, timedelta

import gantt
from clock import done_clock
from fragment import esc
from tracker import WORKFLOW, change_keys, issue_key, launch_row, launcher
# The Log line grammar and the moves read with it live in tracker_log.py, which draws nothing. `moves` is imported
# here as well, so `flow_chart.moves` still names it.
from tracker_log import RELAUNCH_STAGE, Move, moves  # noqa: F401

H = timedelta(hours=1)
# (title, behind now, ahead of now), after the Flow artboard.
WINDOWS = (("24 hours", 6 * H, 18 * H), ("7 days", 48 * H, 120 * H))
COLOURS: dict[str, str | tuple[str, str]] = {stage: f"var(--stage-{stage})" for stage in gantt.PALETTE}
# #217/#226: the chart's own bucket order, earliest-first within a group. A finished row's word is
# "merged" or "closed" (task_forge.forge_ends); both read as merged. "open" is only the queued loop's
# literal status; everything else moved (whatever its stage or tracker state word) counts as running.
BUCKETS = ("merged", "approved", "running", "needs input", "queued")


def _bucket(status: str) -> str:
    if status in ("merged", "closed"):
        return "merged"
    if status in ("approved", "needs input"):
        return status
    if status == "open":
        return "queued"
    return "running"


def _clock(at: datetime, now: datetime) -> str:
    return f"~{at:%H:%M}" if at.date() == now.date() else f"{at:%a}"


def _ahead(stages: list[str], start: datetime, end: datetime, title: str = "") -> list[gantt.Segment]:
    # ponytail: the estimate split evenly over the stages left; per-stage durations once the Log has enough of them.
    step = (end - start) / len(stages)
    return [gantt.Segment(start + step * i, start + step * (i + 1), s, "forecast", title) for i, s in enumerate(stages)]


def _refs(tasks, log: list[Move], home):
    """Look up a row's issue or change, bridging change-only passes only through a unique issue."""
    named = {text: change_keys(text, home) for text in [*(t.item for t in tasks), *(m.name for m in log)]}
    pairs = list(named.values()) + [(issue_key(t.issue, home), named[t.item][1])
                                   for t in tasks if t.issue.strip() and not t.workflow]
    issues = {}
    for issue, change in pairs:
        if issue and change:
            issues.setdefault(change, set()).add(issue)
    change_issues = {change: next(iter(keys)) if len(keys) == 1 else "" for change, keys in issues.items()}

    def ref_of(task, text: str) -> str:
        if task and task.issue.strip() and not task.workflow:
            return issue_key(task.issue, home)
        if task and not task.workflow:
            return ""
        issue, change = named[task.item if task else text]
        return issue or change_issues.get(change, change)

    return ref_of


def build(log: list[Move], tasks, lanes: dict, held: dict[str, str], ends: dict[str, datetime], now: datetime,
          durations: dict[str, timedelta] | None = None, slots: int = 1,
          approved: frozenset[str] = frozenset(), ended: dict[str, tuple[datetime, str]] | None = None,
          home=None) -> list[tuple[str, gantt.Row]]:
    """(status, row) per task with a move, first moved first, then the queued tasks in tracker order. `tasks` are
    tracker rows, later ones winning; `held` maps each held task's name (or item when nameless) to what holds it (task_forge.hold_causes); `ends` maps a task's item to its estimated end;
    `durations` maps a queued task's item to how long it will take, and `slots` is how many run at once;
    `approved` names the tasks whose open change is approved, labelled `approved · merge ~HH:MM` by their end;
    `ended` maps a task's name to (time, word) — its change's forge merge or its closed issue's close time — which
    ends its row (#220). Status is that word, `needs input`, `approved`, or the task's state word (`running` when `open`). Every bar's title names the worker
    whose launch was running when it began (now, for a forecast or hold bar), else the task's owner cell, shown as
    the board shows it (#192, #193). `home` resolves issue keys as in the board's source cache."""
    by_name = {t.name.strip().casefold(): t for t in tasks if t.name.strip()}
    # A move names a task by its name or its item; the name wins.
    find = {**{t.item.strip().casefold(): t for t in tasks if t.item.strip()}, **by_name}
    row_of = launcher(tasks)
    ref_of = _refs(tasks, log, home)

    def key_of(m: Move) -> str:
        t = find.get(m.name.strip().casefold()) or (row_of(m.name)[0] if m.launch else None)
        return (t.name.strip() or t.item.strip()).casefold() if t else m.name.casefold()

    terminal = {lane.stages[-1] for lane in lanes.values()}
    horizon = now + WINDOWS[-1][2]
    busy: list[datetime] = []
    log = sorted(log, key=lambda m: m.at)
    first, runs = {}, {}  # runs: key → [worker, start, end or None] per launch
    for m in log:
        key = key_of(m)
        if not m.launch:
            first.setdefault(key, m.at)
        elif m.stage == "implement":
            runs.setdefault(key, []).append([m.worker, m.at, None])
        elif run := next((r for r in reversed(runs.get(key, ())) if r[0] == m.worker and r[2] is None), None):
            run[2] = m.at

    # Only runs without an explicit end fall back to the next launch on their task.
    # Resolve this after reading all stops so explicitly overlapping runs keep their ends.
    for task_runs in runs.values():
        for run, successor in zip(task_runs, task_runs[1:]):
            if run[2] is None:
                run[2] = successor[1]

    def owner(key: str, at: datetime, task) -> str:
        running = [w for w, start, end in runs.get(key, ()) if start <= at and (end is None or at < end)]
        return running[-1] if running else task.shown_owner if task else ""

    log = [m for m in log if not (m.launch and (f := first.get(key_of(m))) and m.at >= f)]
    out, groups = [], {}
    for m in log:
        groups.setdefault(key_of(m), []).append(m)
    for key, mine in groups.items():
        task, last = find.get(key) or find.get(mine[-1].name.strip().casefold()), mine[-1]
        stop = now
        if task and task.kind == "done":
            # (#196) no clock: stop at its own last move, so it draws no bar past that move.
            # (#213) a clock: its latest occurrence at or before now, not a same-date occurrence that
            # may not have happened yet.
            if t := task.state_time:
                stop = done_clock(t, now, now.tzinfo)
            else:
                stop = last.at
        finish = task and (ended or {}).get(task.name.strip())
        hold_key = (task.name.strip() or task.item.strip()) if task else ""
        is_held = bool(task and hold_key in held)
        boundary = last.at
        # A launcher stop ends its run but leaves the row at its prior working stage.
        mine = [m for m in mine if not (m.launch and m.stage == RELAUNCH_STAGE)]
        last = mine[-1]
        # #227: a held row's last stage bar ends at its own last move — no dangling bar reading as work
        # in progress — and the hold bar alone carries it from now through the window.
        segs = []
        for m, nxt in zip(mine, mine[1:] + [None]):
            if m.stage in terminal:
                continue
            e = nxt.at if nxt else stop
            if m.launch and m.stage == "implement":
                # Concurrent launches have separate boundaries and retain their own worker titles.
                run = next(r for r in runs[key] if r[0] == m.worker and r[1] == m.at)
                e = min(run[2] or stop, first.get(key, stop))
                if is_held:
                    e = min(e, boundary)
            elif nxt is None and is_held:
                continue
            if e > m.at:
                segs.append(gantt.Segment(m.at, e, m.stage, "done",
                                          m.worker if m.launch and m.stage == "implement" else owner(key, m.at, task)))
        if finish:
            at, word = finish
            segs = [replace(g, end=min(g.end, at)) for g in segs if g.start < at]
        lane = lanes.get(task.lane.strip()) if task else None
        is_approved = bool(task and task.name.strip() in approved)
        if finish:
            status, note = word, f"{word} {at:%H:%M}"
        elif last.stage in terminal:
            status, note = "merged", f"merged {last.at:%H:%M}"
        elif is_held:
            segs.append(gantt.Segment(now, now + WINDOWS[-1][2], last.stage, "hold", owner(key, now, task)))
            status, note = "needs input", f"needs input · {held[hold_key]}"
        elif task and lane and last.stage in lane.stages and (end := ends.get(task.item)) and end > now:
            ahead = [last.stage] + [s for s in lane.stages[lane.stages.index(last.stage) + 1:] if s not in terminal]
            segs += _ahead(ahead, now, end, owner(key, now, task))
            busy.append(end)
            # #217: a task not yet marked `running` but already moving through the lane still counts as
            # running, never as queued (which is `open` with no move at all — the loop below).
            status = "running" if task.kind == "open" else task.kind
            note = f"approved · merge {_clock(end, now)}" if is_approved else f"{last.stage} · {_clock(end, now)}"
        else:
            kind = task.kind if task else ""
            status = "approved" if is_approved else ("running" if kind == "open" else kind)
            note = "approved" if is_approved else last.stage
        out.append((status, gantt.Row(ref_of(task, last.name), task.label if task else launch_row(last.name, ())[1],
                                      note, tuple(segs))))
    # Each slot is free once the forecast holding it ends; more forecasts than slots wait for the (n-k+1)th end.
    slots = max(1, slots)
    busy += [now] * (slots - len(busy))
    heapq.heapify(busy)
    moved = set(groups)
    for t in tasks:
        key, lane = t.name.strip().casefold(), lanes.get(t.lane.strip())
        if (not key or by_name[key] is not t or key in moved or t.kind != "open" or t.name.strip() in held
                or not lane or t.stage.strip() != lane.stages[0] or not (took := (durations or {}).get(t.item))):
            continue
        while len(busy) > slots:
            heapq.heappop(busy)
        start = heapq.heappop(busy)
        if start >= horizon:
            break
        heapq.heappush(busy, start + took)
        stages = [s for s in lane.stages if s not in terminal]
        out.append((t.kind, gantt.Row(ref_of(t, t.item), t.name.strip(), f"queued · {_clock(start + took, now)}",
                                      tuple(_ahead(stages, start, start + took, owner(key, now, t))))))
    # #226: rows group merged, approved, running, needs input, queued, earliest move first within a group.
    far_future = now + WINDOWS[-1][2] * 1000  # a row with no segments at all sorts last in its bucket
    return sorted(_merge_issues(out),
                  key=lambda sr: (BUCKETS.index(_bucket(sr[0])), min((g.start for g in sr[1].segments), default=far_future)))


def _parse_store_dt(raw: str | None, fallback: datetime) -> datetime:
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None and fallback.tzinfo is not None:
                dt = dt.replace(tzinfo=fallback.tzinfo)
            elif dt.tzinfo is not None and fallback.tzinfo is not None:
                dt = dt.astimezone(fallback.tzinfo)
            return dt
        except ValueError:
            pass
    return fallback


def build_from_store(db, now: datetime, *, include_hold_bars: bool = False) -> list[tuple[str, gantt.Row]]:
    """Build one Gantt row per store issue with all its `(issue, stage, attempt)` segments (M6 item 1, Exit S2)."""
    items = db.items()
    all_segments = db.segments()
    all_decisions = db.decisions()
    segs_by_key: dict[str, list] = {}
    for seg in all_segments:
        segs_by_key.setdefault(seg.issue_key, []).append(seg)
    open_decs_by_key: dict[str, list] = {}
    for dec in all_decisions:
        if dec.status == "open":
            open_decs_by_key.setdefault(dec.issue_key, []).append(dec)

    out: list[tuple[str, gantt.Row]] = []
    for item in items:
        key = item.issue_key
        raw_segs = segs_by_key.get(key, [])
        if not raw_segs and item.forge_state == "closed":
            continue
        resume = db.resume(key)
        runs = (resume.get("runs") or {}) if isinstance(resume, dict) else {}
        g_segs: list[gantt.Segment] = []
        for s in raw_segs:
            start = _parse_store_dt(s.started_at, now)
            if s.ended_at:
                end = _parse_store_dt(s.ended_at, now)
                kind = "done"
            else:
                end = now
                kind = "running"
            if end < start:
                end = start
            run_info = runs.get(f"{s.stage}-{s.attempt}") or {}
            worker_title = str(run_info.get("runner") or s.agent_type or s.runtime or "")
            g_segs.append(gantt.Segment(start, end, s.stage, kind, worker_title))

        short_ref = f"#{key.rpartition('#')[2]}" if "#" in key else key
        stage = item.stage or (raw_segs[-1].stage if raw_segs else "implement")
        has_open_dec = bool(open_decs_by_key.get(key))
        is_held = bool(item.human_review or has_open_dec)
        if item.forge_state == "closed":
            status, note = "merged", "closed"
        elif is_held:
            cause = "decision" if has_open_dec else stage
            status, note = "needs input", f"needs input · {cause}"
            if include_hold_bars:
                g_segs.append(gantt.Segment(now, now + WINDOWS[-1][2], stage, "hold", ""))
        elif item.review_state == "approved":
            status, note = "approved", "approved"
        elif any(s.ended_at is None for s in raw_segs):
            status, note = "running", stage
        elif raw_segs:
            status, note = "running", stage
        else:
            status, note = "open", f"queued · {stage}"
        row = gantt.Row(
            ref=short_ref,
            name=item.title or key,
            note=note,
            segments=tuple(g_segs),
            href=f"/issues/{key}",
            ref_title=item.title or key,
        )
        out.append((status, row))
    far_future = now + WINDOWS[-1][2] * 1000
    return sorted(
        out,
        key=lambda sr: (BUCKETS.index(_bucket(sr[0])), min((g.start for g in sr[1].segments), default=far_future)),
    )


def from_store(db, start: datetime, end: datetime) -> list[gantt.Row]:
    """Return Gantt rows from the store overlapping `[start, end]`, one row per issue with `(issue, stage, attempt)` segments."""
    rows = [row for _, row in build_from_store(db, end, include_hold_bars=False)]
    return [
        r for r in rows
        if not r.segments or any(g.end >= start and g.start <= end for g in r.segments)
    ]


def _merge_issues(rows: list[tuple[str, gantt.Row]]) -> list[tuple[str, gantt.Row]]:
    """Rows that share a non-empty ref (the task's issue) merge into one: every segment, sorted by start; the
    name of whichever row started earliest; the status and note of whichever moved last (highest segment start,
    ties keeping the later row); placed where the first of its rows was. A row with no segments (its only move
    was its lane's last stage) still counts toward the group, but only a row with segments can supply the name
    or the status/note, falling back to the first or last row by position when none of the group has any. When
    the winning row is finished (merged, closed or done), every segment in the group ends no later than that
    row's own last segment end, and a segment starting after that end is dropped (#220); a finished group with no
    segment at all merges with none and is not clipped (#406)."""
    groups: dict[str, list[int]] = {}
    for i, (_, row) in enumerate(rows):
        if row.ref and row.ref != WORKFLOW:
            groups.setdefault(row.ref, []).append(i)
    out, drop = list(rows), set()
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        entries = [rows[i] for i in idxs]
        timed = [e for e in entries if e[1].segments]
        name = min(timed, key=lambda e: e[1].segments[0].start)[1].name if timed else entries[0][1].name
        if timed:
            status, last_row = max(enumerate(timed), key=lambda p: (p[1][1].segments[-1].start, p[0]))[1]
        else:
            status, last_row = entries[-1]
        segs = [g for _, r in entries for g in r.segments]
        if status in ("merged", "closed", "done") and last_row.segments:
            finish = last_row.segments[-1].end
            segs = [replace(g, end=min(g.end, finish)) for g in segs if g.start <= finish]
        segs = tuple(sorted(segs, key=lambda g: g.start))
        out[idxs[0]] = (status, replace(last_row, name=name, segments=segs))
        drop.update(idxs[1:])
    return [item for i, item in enumerate(out) if i not in drop]


def _span(win: gantt.Window) -> str:
    if win.end - win.start <= 24 * H:
        return f"{win.start:%a %H:%M} → {win.end:%a %H:%M}"
    return " → ".join(f"{d:%a} {d.day} {d:%H:%M}" for d in (win.start, win.end))


def section(rows: list[tuple[str, gantt.Row]], now: datetime) -> str:
    """Both charts and one legend, each chart with the rows that reach into its window; empty with no rows."""
    out = []
    for title, before, after in WINDOWS:
        win = gantt.window(now, before, after)
        shown = [(s, r) for s, r in rows if any(g.start < win.end and g.end > win.start for g in r.segments)]
        if not shown:
            continue
        counts = " · ".join(f"{n} {word}" for word in BUCKETS if (n := sum(_bucket(s) == word for s, _ in shown)) > 0)
        out.append(f'<h2>Flow · {title}</h2>\n<div class="meta">{esc(counts)} · {_span(win)}</div>\n'
                   f"{gantt.render([r for _, r in shown], win)}")
    return "\n".join(out + [gantt.legend(COLOURS)]) + "\n" if out else ""


def css() -> str:
    """gantt.py's stylesheet on the board's stage tokens, with a phone layout of the Phone artboard's widths."""
    return gantt.css(COLOURS) + (".gantt-legend .gantt-forecast:not([data-cat]){--c:var(--stage-implement)}\n"
                                 ".gantt-legend{border-top:1px solid var(--gantt-edge);padding-top:5px}\n"
                                 "@media (max-width:520px){.gantt,.gantt-legend{--gantt-label:96px;--gantt-note:0px}"
                                 ".gantt-note,.gantt-ref{display:none}.gantt-axis{font-size:8.5px}}\n")

"""Render a tracker-based daily board without opening a server or browser.

The page is the Flow board (Flow.dc.html): header, tiles, BUILD, the MERGE ORDER and WORKERS panels,
and the Flow charts. Decisions live on /decisions, which the NEEDS INPUT tile opens (#895). It carries tracker and deadline metadata and ticks its clocks client-side.
The renderer writes the board HTML and prints its path and summary.
"""

from __future__ import annotations


import argparse
import hashlib
import html
import re
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from decision_actions import current_rows, state_of, tab_counts as store_tab_counts
from estimate import (ALL_SIZES, NO_ESTIMATE, SIZES, day_bar, estimates, history, horizon,  # noqa: E402
                      nearest_deadline, queue_durations, task_end as _end)
from md import BULLET, section as _section, unquote as _unquote  # noqa: E402
from settings import Kanban, SettingsError, load as load_settings  # noqa: E402
from task_forge import (EXHAUSTED_CHIP, current_tasks, drifts, effective_stage, epic_risk, forge_ends,  # noqa: E402
                        held_names, hold_began, hold_causes, looping, retry_exhaustion, tab_counts,
                        task_changes, went_held, worker_names)
from backlog_ref import store_label  # noqa: E402
from tracker import TRAILING_NUMBER, Task, change_keys, issue_key, issue_number, parse_tracker  # noqa: E402
from workspace import (Config, ConfigError, EngineStatus, daily_trackers, engine_health, looked_before,  # noqa: E402
                       read_config, read_engine_status, with_decision_deadlines, with_workspace_decision_deadlines)
import board_sources  # noqa: E402
import columns  # noqa: E402
import decision_page  # noqa: E402
from page_common import markdown_inline  # noqa: E402
from decision_page import write_if_changed  # noqa: E402
from fragment import FONTS, TAB_CSS, tab_bar, theme, issue_href  # noqa: E402
import flow_chart  # noqa: E402
import panels  # noqa: E402
from task_forge import PageData, live_worker_keys, source_key, source_workers, source_worker_tasks  # noqa: E402
import tracker_log  # noqa: E402

LONG_ITEM = 80
RESUME_CAP = 300    # writer limit from agent rules
REQ_LINE = re.compile(r"^(\s*)- \[([ xX])\]\s+(.*?)\s*$")
EVIDENCE = " — evidence: "


@dataclass(frozen=True)
class Req:
    text: str
    done: bool
    evidence: str
    depth: int


# --- parsing -------------------------------------------------------------------------------------


def parse_requirements(text: str) -> list[tuple[str, list[Req]]]:
    """Checkbox lines grouped under `## ` headings; indentation gives depth; anything else is a note and ignored."""
    groups: list[tuple[str, list[Req]]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            groups.append((line[3:].strip(), []))
            continue
        m = REQ_LINE.match(line)
        if not m:
            continue
        if not groups:
            groups.append(("Requirements", []))
        body, _, evidence = m.group(3).partition(EVIDENCE)
        groups[-1][1].append(Req(body.strip(), m.group(2) != " ", evidence.strip(), len(m.group(1).expandtabs(2)) // 2))
    return [(name, reqs) for name, reqs in groups if reqs]


# `- Verified 16:52: main = ...` splits at the first colon, and that colon is inside the clock read:
# the key came out `verified 16` and the value `52: main = ...`, so nothing ever matched `verified`
# and the line carrying the day's checked facts was silently absent from the board. These put it back.
LABEL_CLOCK = re.compile(r"^(.*\S)\s+(\d{1,2})$")
VALUE_CLOCK = re.compile(r"^(\d{2}):\s*(.*)$")


def parse_resume(text: str) -> dict[str, str]:
    """The `## Resume` block as `key: value` pairs, keys lowercased. No block, no keys, no error."""
    out: dict[str, str] = {}
    for line in _section(text, "## Resume"):
        m = BULLET.match(line)
        if not m:
            continue
        key, value = _unquote(m.group(2)).lower(), m.group(3).strip()
        head, tail = LABEL_CLOCK.match(key), VALUE_CLOCK.match(value)
        if head and tail:
            key, value = head.group(1), f"{head.group(2)}:{tail.group(1)} · {tail.group(2)}"
        out[key] = value
    return out


# Reading order for the fields the ruleset names, and nothing more: a field outside this tuple is
# drawn after them rather than dropped. It used to be an allowlist of four, so the day the ruleset
# gained `Re-arm` the renderer began discarding it without a word.
RESUME_STRIP = ("as of", "in flight", "next", "waiting on", "re-arm", "verified")


def resume_fields(block: dict[str, str]) -> list[str]:
    """The fields that will be drawn, in reading order. The renderer and the summary line share it.

    Finding 65: a count of what was parsed is not a count of what was drawn, and only the second one
    is a check. `resume=N fields` is taken from here so the two can never disagree again.
    """
    return [k for k in RESUME_STRIP if block.get(k)] + [k for k in block if k not in RESUME_STRIP and block[k]]


# --- rendering -----------------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _tasks(n: int) -> str:
    return f"{n} {'task' if n == 1 else 'tasks'}"


def stage_order(lanes: dict) -> list[str]:
    """Every lane's stages in one order that keeps each lane's own: a new stage lands after its predecessor, a lane's
    leading new stages before its first known one, and a lane sharing none after the rest. graphlib's topological
    order would interleave two lanes that share no stage."""
    order: list[str] = []
    for lane in lanes.values():
        at, pending = None, []
        for stage in lane.stages:
            if stage in order:
                i = order.index(stage)
                order[i:i] = pending
                at, pending = i + len(pending) + 1, []
            elif at is None:
                pending.append(stage)
            else:
                order.insert(at, stage)
                at += 1
        order += pending
    return order


PIPELINE_MARK = {"passed": "passed", "failed": "failed", "running": "pending", "pending": "pending"}


def change_marks(changes: tuple[board_sources.Change, ...], queue: dict[str, int] | None = None,
                 shared: dict[str, list[str]] | None = None) -> tuple[columns.Mark, ...]:
    return tuple(m for c in changes for m in (
        *((columns.Mark(c.pipeline, PIPELINE_MARK[c.pipeline]),) if c.pipeline in PIPELINE_MARK else ()),
        *((columns.Mark("approved", "approved"),) if c.approved else ()),
        *((columns.Mark(f"queue {queue[c.ref]}", "queue"),) if queue and c.ref in queue else ()),
        *(columns.Mark(f"shares file with {ref}", "shared") for ref in (shared or {}).get(c.ref, ()))))


def merged_today(sources: board_sources.Sources, now: datetime) -> list[board_sources.Change]:
    return sorted((c for c in sources.changes.values() if c.state == "merged" and c.merged_at
                   and c.merged_at.astimezone(now.tzinfo).date() == now.date()), key=lambda c: c.merged_at, reverse=True)


def build_columns(tasks: list[Task], lanes: dict | None, kanban: Kanban | None = None,
                  sources: board_sources.Sources = board_sources.EMPTY, now: datetime | None = None,
                  held: dict[Task, str] | None = None, loops: set[Task] | frozenset[Task] = frozenset(),
                  live_keys: frozenset[str] = frozenset(), live_tasks: set[Task] | frozenset[Task] = frozenset()) -> list[columns.Column]:
    """The Build board: a column per lane stage, a card per task at it. Sources add each card's changes and marks, and
    a `main` column of the changes merged today; `held` (task_forge.hold_causes) flags a card NEEDS INPUT."""
    lanes = lanes or {}
    held = hold_causes(tasks, kanban, sources, lanes, frozenset()) if held is None else held
    gates = {g for lane in lanes.values() for g in lane.gates}

    def issue_url(ref: str, cell: str = "") -> str:
        known = sources.issues.get(ref)
        return known.url if known else cell.strip() if cell.strip().startswith(("https://", "http://")) else ""

    def page(cell: str, other: str) -> str:
        """A card name's link: its task page when the issue ends in a number, as Flow rows link, else `other` (#194)."""
        raw = cell.strip()
        if ":" in raw and "#" in raw and not raw.startswith(("http://", "https://")):
            return issue_href(raw)
        m = TRAILING_NUMBER.search(raw)
        return f"/issues/{int(m[1])}" if m else other

    def change_href(c: board_sources.Change) -> str:
        """A change's own page: the task page of its own first named issue, or its forge link when it names
        none — pages.py serves no route for a change itself (#209)."""
        return page(c.issues[0] if c.issues else "", c.url)

    workers = worker_names(sources)
    # Approved, passing, unshared changes first, then change number; drafts still share files.
    open_ = [c for c in sources.changes.values() if c.state == "open"]
    shared = {c.ref: sorted({o.ref for o in open_ if o is not c and set(o.files) & set(c.files)}) for c in open_}
    ready = sorted((c for c in open_ if not c.draft),
                   key=lambda c: (not c.approved, c.pipeline != "passed", bool(shared[c.ref]),
                                  int(m[1]) if (m := TRAILING_NUMBER.search(c.ref)) else 0))
    queue = {c.ref: i for i, c in enumerate(ready, 1) if c.approved}

    def card(task: Task, stage: str | None = None) -> columns.Card:
        changes = task_changes(task, sources)
        url = issue_url(issue_key(task.issue, sources.home), task.issue)
        raw_issue = task.issue.strip()
        full_key = ":" in raw_issue and "#" in raw_issue and not raw_issue.startswith(("http://", "https://"))
        issue = store_label(raw_issue, sources.home) if full_key else (issue_key(task.issue, sources.home) if changes or url else raw_issue)
        refs = ((issue, page(raw_issue if full_key else issue, url)),) * bool(issue) + tuple((c.ref, change_href(c)) for c in changes)
        lane = lanes.get(task.lane.strip())
        finished = task.kind == "done" or (lane and lane.stages and stage == lane.stages[-1])
        marks = (change_marks(changes, queue, shared)
                 + ((columns.Mark("decision pending", "decision"),) if held.get(task) == "decision" else ())
                 + ((columns.Mark("looping", "looping"),) if task in loops and not finished else ())
                 + ((columns.Mark("drift", "drift"),) if drifts(task, kanban, sources, stage) else ()))
        owner = task.shown_owner
        live = task.issue in live_keys or task in live_tasks
        if live and task.kind == "done":
            marks += (columns.Mark("running", "running"),)
        if live and task.kind != "done":
            chip, kind = "running", "running"
        elif held.get(task) == EXHAUSTED_CHIP:
            chip, kind = EXHAUSTED_CHIP, "waiting"
        elif task.kind == "waiting" and task in held:
            chip, kind = "needs input", task.kind
        else:
            chip, kind = task.state, task.kind
        return columns.Card(task.label, kind, refs, owner, chip, marks,
                            flag=columns.Mark("NEEDS INPUT", "hold") if task in held else None,
                            href=page(task.issue, next((c.url for c in changes if c.state == "open"), url)),
                            owner_href="/workers" if owner in workers else "")

    laned = [t for t in tasks if t.lane.strip() and t.stage.strip()]
    at = {t: effective_stage(t, sources, lanes) for t in laned}
    order = list(dict.fromkeys(stage_order(lanes) + list(at.values())))
    # No PR column: a card at `pr` draws in the stage after it, normally review.
    into = {"pr": order[order.index("pr") + 1]} if "pr" in order[:-1] else {}
    order = [s for s in order if s not in into]
    cols = [columns.Column(stage, tuple(card(t, at[t]) for t in laned if into.get(at[t], at[t]) == stage),
                           *(("gate", "gate") if stage in gates else ())) for stage in order]
    merged = merged_today(sources, now) if now else []
    if merged:
        cards = tuple(columns.Card(c.title, "merged today", tuple((i, page(i, issue_url(i))) for i in c.issues[:1]) + ((c.ref, change_href(c)),),
                                   f"merged {c.merged_at.astimezone(now.tzinfo):%H:%M}", "done",
                                   href=change_href(c)) for c in merged)
        at = next((i for i, c in enumerate(cols) if c.name == "main"), None)
        if at is None:
            cols.append(columns.Column("main", cards))
        else:
            cols[at] = replace(cols[at], cards=cols[at].cards + cards)
    return cols


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# The panels' and the new marks' colours are the board's own tokens, so the dark scheme reaches them.
PANEL_TOKENS = {"ink": "var(--fg)", "muted": "var(--muted)", "card": "var(--surface)", "rule": "var(--line)",
                "edge": "var(--brass)", "link": "var(--brass)", "hot": "var(--dl)", "hot-ink": "var(--surface)"}
PANEL_COLOURS = {"all": "var(--fg)", "blocked": "var(--dl)", "approved": "var(--stage-review)",
                 "running": "var(--stage-implement)", "drift": "var(--dl)", "orphaned": "var(--muted)"}
CARD_COLOURS = {**columns.PALETTE, "merged today": columns.PALETTE["done"],
                "approved": ("var(--stage-review)", "var(--surface)", "1px solid var(--stage-review)"),
                "passed": ("var(--surface)", "var(--fg)", "1px solid var(--stage-review)"),
                "failed": ("var(--surface)", "var(--dl)", "1px solid var(--dl)"),
                "pending": ("var(--surface)", "var(--fg)", "1px solid var(--stage-pr)"),
                "drift": ("var(--surface)", "var(--dl)", "1px dashed var(--dl)"),
                "queue": ("var(--surface)", "var(--stage-review)", "1px solid var(--stage-review)"),
                "shared": ("var(--surface)", "var(--brass)", "1px solid var(--brass)"),
                "decision": ("var(--surface)", "var(--dl)", "1px solid var(--dl)"),
                "looping": ("var(--surface)", "var(--stage-fix)", "1px solid var(--stage-fix)")}


def flow_tiles(total: int, held: int, sources: board_sources.Sources, running: int, drift: int, orphaned: int,
               *, actual_running: int | None = None) -> list[panels.Tile]:
    """Six counts in the artboard's order and words, each linking to where its items are listed. ALL counts every
    task; NEEDS INPUT the held cards; RUNNING workers, or running tasks when no worker source is read."""
    approved = sum(c.state == "open" and c.approved for c in sources.changes.values())
    if actual_running is None:
        actual_running = len(source_workers(sources)) if sources.workers or "workers" in sources.fetched else running
    return [panels.Tile("ALL", total, "flow", "all"), panels.Tile("NEEDS INPUT", held, "flow", "blocked", "/decisions"),
            panels.Tile("RUNNING", actual_running, "flow", "running"),
            panels.Tile("APPROVED", approved, "flow", "approved"),
            panels.Tile("ORPHANED", orphaned, "flow", "orphaned"), panels.Tile("DRIFT", drift, "flow", "drift")]


def _tasks_from_store(db: PageData, cfg: Config, lanes: dict | None, kanban: Kanban | None,
                      today: date) -> tuple[list[Task], dict[Task, str], set[Task], list]:
    store_items = db.items()
    store_segments = db.segments()
    store_decisions = current_rows(db.decisions())  # rows are append-only: each decision's latest
    segs_by_key: dict[str, list] = {}
    for seg in store_segments:
        segs_by_key.setdefault(seg.issue_key, []).append(seg)
    tasks: list[Task] = []
    held: dict[Task, str] = {}
    loops: set[Task] = set()
    default_lane = next(iter(lanes)) if lanes else "build"
    for item in store_items:
        lane_name = item.workflow or default_lane
        lane_obj = (lanes or {}).get(lane_name)
        stage_name = item.stage or (lane_obj.stages[0] if lane_obj and lane_obj.stages else "implement")
        item_segs = segs_by_key.get(item.issue_key, [])
        open_seg = next((s for s in reversed(item_segs) if s.ended_at is None), None)
        open_decs = [d for d in store_decisions if d.issue_key == item.issue_key and state_of(d).status in ("open", "blocked")]
        exhausted = retry_exhaustion(db.resume(item.issue_key), item_segs, db.log(item.issue_key))
        is_held = bool(item.human_review or open_decs or exhausted or (kanban and kanban.holds(stage_name)))
        if item.forge_state == "closed":
            state = "done"
        elif exhausted:
            state = f"waiting · {EXHAUSTED_CHIP}"
        elif is_held:
            state = "waiting"
        elif open_seg:
            state = "running 00:00"
        else:
            state = "open"
        owner = (getattr(open_seg, "runner", "") if open_seg else "") or (cfg.user if is_held else "unassigned")
        t = Task(item=item.title or item.issue_key, owner=owner, state=state, since=today.isoformat(),
                 due="", checklist="", name=item.title or item.issue_key, lane=lane_name,
                 stage=stage_name, issue=item.issue_key)
        tasks.append(t)
        if exhausted and t.kind != "done":
            held[t] = EXHAUSTED_CHIP
        elif open_decs and t.kind != "done":
            held[t] = "decision"
        elif is_held and t.kind != "done":
            held[t] = stage_name or cfg.user
        if sum(1 for s in item_segs if s.stage.strip().casefold() == "fix") >= 2:
            loops.add(t)
    return tasks, held, loops, store_decisions


def render(tracker_text: str, cfg: Config, now: datetime, lanes: dict | None = None,
           tracker_day: date | None = None, decisions: list[tuple[str, dict | None, datetime | None, str]] | None = None,
           kanban: Kanban | None = None, sources: board_sources.Sources = board_sources.EMPTY, answered: int = 0,
           tracker_at: datetime | None = None, stage_log: list[tuple[date, str]] | None = None, slots: int = 1,
           pending_refs: dict[str, datetime | None] | None = None, looked: datetime | None = None,
           look_log: list[tuple[date, str]] | None = None,
           loop_log: list[tuple[date, str]] | None = None,
           db: PageData | None = None,
           engine_status: EngineStatus | None = None) -> str:
    """The board page: header, tiles, BUILD (the stage columns), and the Flow charts.
    `stage_log` is (day, tracker text) for the earlier days the Flow charts reach back over; `slots` is how many queued tasks run at once, `[workers] max_concurrency`.
    `pending_refs` is decision_page.held_refs; `looked`, the look before the latest (workspace.looked_before), and
    `look_log` the earlier days needed for hold starts; `loop_log` the active tasks' older fix history."""
    cfg = with_decision_deadlines(cfg, tracker_text, tracker_day or now.date())
    sha = hashlib.sha256(tracker_text.encode()).hexdigest()
    tracker = parse_tracker(tracker_text)
    today, zone = now.date(), cfg.zone
    # A standing session with no task is not a task.
    tasks = [task for task in tracker.tasks if not task.standing]
    store_held: dict[Task, str] = {}
    store_loops: set[Task] = set()
    store_decisions: list = []
    if db is not None:
        s_tasks, store_held, store_loops, store_decisions = _tasks_from_store(db, cfg, lanes, kanban, today)
        tasks = s_tasks
    active = [task for task in tasks if task.kind != "done"]
    nearest = nearest_deadline(cfg, now)
    hist = history(tasks, cfg, now)
    est = estimates(active, cfg, now, hist)

    unowned = [task for task in active if task.kind == "orphaned"]
    running = [task for task in active if task.kind == "running"]

    issues = sum(1 for t in tasks if t.issue.strip() and not t.workflow)
    changes = sum(1 for c in sources.changes.values() if c.state == "open")
    # A task's stage for drift, computed once, keyed by the task: every nameless task shares the blank name.
    stages = {t: effective_stage(t, sources, lanes or {}) for t in tasks}
    # #228: what holds each task, decided once, so the BUILD flag, the NEEDS INPUT tile and the Flow note agree.
    pending_refs = dict(pending_refs or {})
    for d in store_decisions:
        if state_of(d).status in ("open", "blocked"):
            pending_refs.setdefault(d.issue_key, None)
            pending_refs.setdefault(source_key(d.issue_key, sources.home), None)
    held = {**hold_causes(tasks, kanban, sources, lanes or {}, pending_refs), **store_held}
    held = {t: why for t, why in held.items() if t.kind != "done"}
    # Each snapshot is parsed once even when Flow, holds and loop marks all need it.
    flow_log = [*(stage_log or []), (tracker_day or today, tracker_text)]
    all_log = dict([*(loop_log if loop_log is not None else look_log or []), *(look_log or []), *flow_log])
    rows = {d: tracker.tasks if d == (tracker_day or today) else parse_tracker(text).tasks for d, text in all_log.items()}
    moves = {d: tracker_log.moves(text, d, zone) for d, text in all_log.items()}
    flow_moves = [m for day, _ in flow_log for m in moves[day]]
    earlier = list(rows.items())
    loops = looping(tasks, [m for ms in moves.values() for m in ms], lanes or {}, sources.home, earlier) | store_loops
    live_keys = frozenset(s.issue_key for s in db.segments() if s.ended_at is None) if db is not None else frozenset()
    build_cols = build_columns(tasks, lanes, kanban, sources, now, held, loops, live_keys, source_worker_tasks(sources, tasks))
    inline = lambda text: markdown_inline(text, decision_page.Linker(cfg.backlog).resolve)  # noqa: E731
    build_html = (f'<section id="flow"><h2>Build</h2>\n<div class="meta">{_plural(issues, "issue")} · {_plural(changes, "merge request")}</div>\n'
                  f'{columns.render(build_cols, inline=inline, linked_inline=lambda text: markdown_inline(text, links=False))}'
                  '</section>\n')
    known = [t for d, _ in stage_log or [] for t in rows[d]] + tasks
    # The board's estimate, by item: where the Flow draws a row's end and what an epic's forecast is (#230).
    ends = {t.item: end[0] for t in active if (end := _end(t, cfg, now, now, est))[1] in ("due", "derived")}
    if db is not None:
        navigation = store_tab_counts(db.items(), db.decisions(), db.segments(), db.resumes)
    else:
        navigation = tab_counts(tracker_text, cfg, now, lanes or {}, kanban, sources, pending_refs, looked,
                                [*(look_log or []), *(stage_log or [])], slots, held=held, ends=ends, tracker_day=tracker_day)
    if db is not None:
        flow_rows = flow_chart.build_from_store(db, now, include_hold_bars=True)
    else:
        flow_rows = flow_chart.build(flow_moves, known, lanes or {}, held_names(held), ends, now,
                                     queue_durations(active, hist), slots,
                                     frozenset(t.name.strip() for t in tasks
                                               if t.name.strip() and any(c.state == "open" and c.approved for c in task_changes(t, sources))),
                                     ended=forge_ends(tasks, sources, zone), home=sources.home)
    # An issue's row links to its page, pages.py's /issues/<n>.
    issue_cells = {issue_key(t.issue, sources.home): t.issue.strip() for t in known if t.issue.strip() and not t.workflow}

    def flow_ref(row):
        if row.href.startswith("/issues/") and (db is not None or ":" in row.href):
            return row
        if (number := issue_number(row.ref)) is not None:
            return replace(row, ref=f"#{number}", href=f"/issues/{number}", ref_title=issue_cells.get(row.ref, row.ref))
        if change := change_keys(row.ref)[1]:
            return replace(row, ref=f"!{int(change.rstrip('/').rsplit('/', 1)[1])}")
        return row

    flow_html = flow_chart.section([(s, flow_ref(r))
                                    for s, r in flow_rows], now)

    pending = decisions or []
    open_store_decisions = sum(1 for d in store_decisions if state_of(d).status in ("open", "blocked"))
    # (#215, #936) No render clock: pages re-render on request, and `.sources.json` refreshes independently of
    # engine passes. Surface real engine liveness/pass timestamps; never fabricate `tracker HH:MM` on store workspaces.
    health = engine_health(engine_status, now, zone) if engine_status is not None else None
    meta = list(health.meta) if health is not None else []
    if engine_status is None and db is None:
        meta.append(f"tracker {(tracker_at or now).astimezone(zone):%H:%M}")
    elif tracker_at is not None:
        meta.append(f"tracker {tracker_at.astimezone(zone):%H:%M}")
    source_keys = ("merge requests",) if db is not None and not tracker_text.strip() else ("kanban", "merge requests", "workers")
    meta.extend(f"{k} {sources.fetched[k].astimezone(zone):%H:%M}" for k in source_keys if k in sources.fetched)
    meta.append(_tasks(len(tasks)))
    if db is not None and (latest := next((row.message for row in reversed(db.engine_logs)
                                           if row.kind == "policy_change"), "")):
        meta.append(latest)
    warnings = (
        *((health.warning,) if health is not None and health.warning else ()),
        *(f"{k}: {why}" for k, why in sources.errors.items()),
    )
    head = panels.Header(f"Board · {now.strftime('%a %d %b')}", tuple(meta), now, nearest.name, nearest.at, warnings)
    # NEEDS INPUT counts the held tasks, as the BUILD cards are flagged; ALL counts every task (#349).
    tiles = flow_tiles(len(tasks), len(held),
                       sources, len(running), sum(drifts(t, kanban, sources, stages[t]) for t in tasks), len(unowned),
                       actual_running=len(live_worker_keys(sources, db.segments())) if db is not None else None)

    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Board {today.isoformat()}</title>
<link rel="stylesheet" href="{FONTS}">
<meta name="tracker-sha256" content="{sha}">
<style>
{theme()}
:root{{--bg:#f4efe1;--surface:#fbf8ef;--fg:#2f2630;--muted:#6e6470;--line:#ddd3bd;--brass:#a7843e;--dl:#c9533a;--stage-implement:#0072B2;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#000000}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{color-scheme:dark;--bg:#1c1813;--surface:#262019;--fg:#efe6d2;--muted:#b3a791;--line:#3d352a;--brass:#d4b06a;--dl:#f08566;--stage-implement:#3D95D6;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#efe6d2}}:root:not([data-theme="light"]) :is(.gantt,.gantt-legend){{--gantt-ink:var(--fg);--gantt-muted:var(--muted);--gantt-bg:var(--bg);--gantt-rule:var(--line);--gantt-edge:var(--line);--gantt-grid:var(--line);--gantt-past:var(--surface);--gantt-now:var(--dl);--gantt-hold:var(--dl)}}}}
:root[data-theme="dark"]{{color-scheme:dark;--bg:#1c1813;--surface:#262019;--fg:#efe6d2;--muted:#b3a791;--line:#3d352a;--brass:#d4b06a;--dl:#f08566;--stage-implement:#3D95D6;--stage-pr:#56B4E9;--stage-review:#009E73;--stage-triage:#E69F00;--stage-fix:#D55E00;--stage-verify:#CC79A7;--stage-merge:#efe6d2}}
:root[data-theme="dark"] :is(.gantt,.gantt-legend){{--gantt-ink:var(--fg);--gantt-muted:var(--muted);--gantt-bg:var(--bg);--gantt-rule:var(--line);--gantt-edge:var(--line);--gantt-grid:var(--line);--gantt-past:var(--surface);--gantt-now:var(--dl);--gantt-hold:var(--dl)}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 "Alegreya Sans","Gill Sans",system-ui,sans-serif;padding:16px 16px 48px;margin:0 auto}}
h1{{font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-size:26px;font-weight:600;letter-spacing:.04em;line-height:normal;margin:0 0 2px}}
h2{{display:inline-block;font-family:"Cormorant SC","Cormorant Garamond",Georgia,serif;font-size:17px;font-weight:600;letter-spacing:.1em;line-height:normal;text-transform:uppercase;margin:28px 10px 8px 0}}
h2+.meta{{display:inline-block}}
.header{{display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap;margin-bottom:4px}}
.header>.panels-head{{flex:1 1 320px;min-width:0}}
.meta{{color:var(--muted);font-size:12px}}
{TAB_CSS}.board>nav.tabs{{margin-bottom:12px}}
{flow_chart.css() if flow_html else ""}{panels.css(PANEL_COLOURS, PANEL_TOKENS)}{columns.css(CARD_COLOURS)}.columns{{--columns-ink:var(--fg);--columns-muted:var(--muted);--columns-card:var(--surface);--columns-rule:var(--line);--columns-gate-ink:var(--brass);--columns-link:var(--brass);--columns-edge:var(--brass);--columns-bg:color-mix(in srgb,var(--brass) 10%,var(--bg));--columns-gate:color-mix(in srgb,var(--brass) 14%,var(--surface))}}
/* The canonical theme sits above the marker in this block (fragment.theme); these restate what its base rules would
   otherwise change: the board keeps its own box sizing, scrolling, spacing and figures. */
*{{box-sizing:content-box}}
html{{scroll-behavior:auto}}
section{{margin-top:0}}
figure{{margin:0;padding:0;background:none;border:0}}
@media (max-width:420px){{body{{padding:12px 12px 36px}}}}
</style>
<body>
<div class="board" data-rendered-at="{_iso(now)}" data-tz="{_esc(cfg.tz)}" data-deadline="{_iso(nearest.at)}" data-deadline-name="{_esc(nearest.name)}">
{tab_bar("Board", sum(d is not None for _, d, _, _ in pending) + open_store_decisions, **navigation)}<div class="header">{panels.header(head)}</div>
{panels.tiles(tiles)}
{build_html}
{flow_html}</div>
<script>
(function(){{
  var root=document.querySelector('.board'),tz=root.dataset.tz,dl=new Date(root.dataset.deadline);
  function hms(d){{return new Intl.DateTimeFormat('en-GB',{{timeZone:tz,hour:'2-digit',minute:'2-digit',second:'2-digit'}}).format(d);}}
  function pad(n){{return (n<10?'0':'')+n;}}
  function tick(){{
    var now=new Date(),ms=dl-now,sign=ms<0?'-':'',s=Math.floor(Math.abs(ms)/1000);
    root.querySelector('.panels-now').textContent=hms(now);
    root.querySelector('.panels-left').textContent=sign+Math.floor(s/3600)+'h'+pad(Math.floor(s/60)%60)+'m'+pad(s%60)+'s';
  }}
  tick();setInterval(tick,1000);
}})();
</script>
</body>
"""


# --- cli -----------------------------------------------------------------------------------------


def write(root: Path, day: str | None = None, *, now: datetime | None = None, db: PageData | None = None) -> tuple[Path, Config, datetime, str, dict]:
    """Write `<day>-board.html` and `decisions.html` into the pages dir. The page server calls this in
    process; a missing CLAUDE.md, tracker or setting raises ConfigError."""
    if not (root / "CLAUDE.md").is_file():
        raise ConfigError(f"no CLAUDE.md at {root}")
    cfg = read_config(root)
    # To the minute: the board's bytes then change only when something shown does, not with every render (#330).
    now = (now.astimezone(cfg.zone) if now is not None else datetime.now(cfg.zone)).replace(second=0, microsecond=0)
    day = day or now.date().isoformat()
    try:
        tracker_day = date.fromisoformat(day)
    except ValueError:
        raise ConfigError(f"invalid date {day!r}; use YYYY-MM-DD") from None
    tracker = root / cfg.tracker_path(day)
    if not tracker.is_file() and db is None:
        raise ConfigError(f"tracker not found at {tracker}")
    tracker_text = tracker.read_text() if tracker.is_file() else ""
    cfg = with_workspace_decision_deadlines(root, cfg, tracker_text, tracker_day)
    out = (root / cfg.pages_dir if cfg.pages_dir else tracker.parent) / f"{day}-board.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    req_texts = {d.name: ((root / d.requirements).read_text() if (root / d.requirements).is_file() else None) for d in cfg.deadlines if d.requirements}
    try:
        settings = load_settings(root, cfg.settings_path)
    except SettingsError as e:
        raise ConfigError(str(e)) from None
    ctx = decision_page.decision_context(root, tracker_day, now)
    navigation = (store_tab_counts(db.items(), db.decisions(), db.segments(), db.resumes)
                  if db is not None else None)
    pending = decision_page.write_all(out.parent, ctx, day, root, db=db, navigation=navigation)
    reach = (now - flow_chart.WINDOWS[-1][1]).date()
    looked = looked_before(out.parent)
    sources = board_sources.load(out.parent)
    since_day = (looked.astimezone(cfg.zone).date() if looked else now.date())
    bound = min(reach, since_day)
    # Follow active laned tasks backwards only while they occur in consecutive available snapshots.
    remaining = {t for t in parse_tracker(tracker_text).tasks if t.kind != "done" and not t.standing
                 and t.lane.strip() and t.stage.strip()
                 and (lane := settings.lanes.get(t.lane.strip())) and "fix" in lane.stages
                 and effective_stage(t, sources, settings.lanes) != lane.stages[-1]}
    logs = []
    loop_log = []
    for d, path in reversed(daily_trackers(root, cfg)):
        if d >= tracker_day:
            continue
        if d < bound and not remaining:
            break
        text = path.read_text()
        if remaining:
            remaining &= set(current_tasks(list(remaining), parse_tracker(text).tasks, sources.home).values())
            loop_log.append((d, text))
        if d >= bound:
            logs.append((d, text))
    stage_log = sorted((d, text) for d, text in logs if reach <= d)
    look_log = sorted((d, text) for d, text in logs if since_day <= d < reach)
    page = render(tracker_text, cfg, now, lanes=settings.lanes,
                  tracker_day=tracker_day, decisions=pending, kanban=settings.kanban,
                  sources=sources,
                  tracker_at=datetime.fromtimestamp(tracker.stat().st_mtime, cfg.zone) if tracker.is_file() else None,
                  stage_log=stage_log,
                  slots=settings.workers.max_concurrency or 1, pending_refs=decision_page.held_refs(pending, ctx),
                  looked=looked, look_log=look_log, loop_log=sorted(loop_log), db=db,
                  engine_status=read_engine_status(root))
    write_if_changed(out, page)
    return out, cfg, now, tracker_text, req_texts


def main(argv: list[str] | None = None, *, writer=write) -> Path:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="YYYY-MM-DD; default: today in the workspace timezone")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    args = ap.parse_args(argv)
    try:
        out, cfg, now, tracker_text, req_texts = writer(Path(args.root), args.date)
    except ConfigError as e:
        sys.exit(f"render_board: {e}")
    parsed = parse_tracker(tracker_text)
    hist = history(list(parsed.tasks), cfg, now)
    est = estimates([task for task in parsed.tasks if task.kind != "done"], cfg, now, hist)
    bars = [day_bar(task, cfg, now, est) for task in parsed.tasks]
    sized = " ".join(f"{k or '?'}:{hist[k][1]}" for k in (*SIZES, "") if k in hist)
    hist_note = f"{sized} · all:{hist[ALL_SIZES][1]}" if hist else "none"
    horizon_name = horizon(cfg, now)[1]
    no_est = sum(1 for b in bars if b.label == NO_ESTIMATE)
    derived = sum(1 for b in bars if b.end_src == "derived")
    long_items = sum(1 for task in parsed.tasks if len(task.item) > LONG_ITEM)
    block = parse_resume(tracker_text)
    resume_long = sum(1 for k in resume_fields(block) if len(block[k]) > RESUME_CAP)
    # A count printed beside a warning count but not counted as one reads as telemetry: `resume_long=4`
    # sat next to `warnings=0` for three hours while the block degraded, and was read past every time.
    warnings = sum(1 for task in parsed.tasks if task.warning) + sum(1 for s in parsed.sessions if s.warning) + resume_long
    print(out)
    req_counts = ",".join(f"{name}:{sum(r.done for _, rs in parse_requirements(t) for r in rs)}/{sum(len(rs) for _, rs in parse_requirements(t))}" for name, t in req_texts.items() if t is not None)
    missing = sum(1 for t in req_texts.values() if t is None)
    drawn = resume_fields(block)
    resume_note = f"{len(drawn)} fields" if drawn else "none"
    print(f"tasks={len(parsed.tasks)} no_estimate={no_est} derived={derived} warnings={warnings} long_items={long_items} resume={resume_note} resume_long={resume_long} requirements={req_counts or 'none'} requirements_missing={missing} history={hist_note} horizon={horizon_name} tracker_sha256={hashlib.sha256(tracker_text.encode()).hexdigest()[:12]}")
    return out


if __name__ == "__main__":
    main()

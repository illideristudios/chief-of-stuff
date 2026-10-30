"""What the forge's cached sources say about the tracker's tasks: the changes naming a task's issue, the stage a
merged change has moved it to, whether the issue's labels drift from that stage, and when its Flow row ends. Beside
them, who the workers are and whether a stage holds. The board and the task page read these; nothing here draws.
"""

from __future__ import annotations

from collections.abc import Container, Iterable, Mapping
from datetime import date, datetime, time
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import board_sources
import decision_actions
from estimate import estimates, history, task_end
import kanban as kanban_tool
from readiness import EXHAUSTED_CHIP, EXHAUSTED_UNBLOCK, RetryExhaustion, retry_exhaustion
from md import section
from settings import Kanban
import tracker
from tracker import Task, issue_key, launcher
import tracker_log
from tracker_log import Move


def source_key(key: str, home) -> str:
    """Translate an engine identity to the cache's repository-relative key without dropping its namespace."""
    namespace, sep, number = key.rpartition("#")
    if sep and number.isdecimal():
        if namespace.startswith("github:"):
            url = f"https://github.com/{namespace[len('github:'):]}/issues/{number}"
            ref = board_sources.backlog.issue_ref(url, home, any_host=True)
            return ref.label(home) if ref else key.strip()
        if namespace.startswith("gitlab:"):
            return board_sources.store_label(key, home)
        if namespace and "/" in namespace and isinstance(home, board_sources.backlog.Backlog):
            ref = board_sources.backlog.issue_ref(key, home, any_host=True)
            if ref:
                return ref.label(home)
            return key.strip()
    if "://" in key and board_sources.backlog.issue_ref(key, home, any_host=True) is None:
        return key.strip()
    return issue_key(key, home)


def lookup_keys(refs: Iterable[str], home) -> tuple[str, ...]:
    """Cache lookup keys for `refs` under `home`, preserving namespace so cross-repo tasks never match bare `#N`."""
    out: list[str] = []
    for raw in refs:
        text = raw.strip()
        if not text or text == tracker.WORKFLOW:
            continue
        if home is None and text.startswith('#') and text[1:].isdecimal():
            continue  # a bare tracker reference has no verified forge identity
        if (isinstance(home, board_sources.backlog.Backlog) and board_sources.ambiguous_gitlab_label(text)):
            continue  # legacy host labels and nested project shorthand have identical spellings
        # URLs are converted to cache keys; a raw foreign URL in an old change cache must not match.
        candidates = (source_key(text, home),) if "://" in text else (text, source_key(text, home))
        for candidate in candidates:
            if candidate and candidate not in out:
                out.append(candidate)
    return tuple(out)


def tracker_issue_tasks(trackers, number: int, home, full_key: str | None = None):
    """Resolve a numeric route using the latest snapshot naming it; reject multiple identities there."""
    snapshots = [[t for t in tracker.parse_tracker(text).tasks if tracker.issue_number(t.issue) == number]
                 for _, text in trackers]
    if full_key:
        selected = source_key(full_key, home)
    else:
        latest = next((rows for rows in reversed(snapshots) if rows), [])
        identities = {source_key(t.issue, home) for t in latest}
        if len(identities) > 1:
            return None
        selected = next(iter(identities), None)
    return [t for rows in snapshots for t in rows if source_key(t.issue, home) == selected] if selected else []


def hold_cause(task: Task, kanban: Kanban | None, stage: str | None, *, workers: set[str], pending_refs: Container[str],
               home, lane) -> str:
    """What holds the task, or "" when nothing does, at `stage` (its own cell when None): `decision` when a pending
    decision holds its issue (`pending_refs`, keyed as issue_key keys it under `home`), else the stage when [kanban] holds it there, else the person a waiting task waits on.
    The one place a hold is decided, so the BUILD flag, the NEEDS INPUT tile and the Flow note agree (#228). Nothing
    holds a finished task: done, or at its `lane`'s last stage, where a merged change puts it (#240)."""
    stage = task.stage.strip() if stage is None else stage
    if task.kind == "done" or (lane and lane.stages and stage == lane.stages[-1]):
        return ""
    if task.issue.strip() and source_key(task.issue, home) in pending_refs:
        return "decision"
    if kanban is not None and kanban.holds(stage):
        return stage
    if task.kind == "waiting" and task.shown_owner and task.shown_owner not in workers:
        return task.shown_owner
    return ""


def worker_names(sources: board_sources.Sources) -> set[str]:
    return {w.name for w in sources.workers}


def source_workers(sources: board_sources.Sources) -> list[board_sources.Worker]:
    """The distinct live workers shown on the Workers page, excluding idle sessions."""
    found = {}
    for worker in sources.workers:
        if worker.kind == "session" and not worker.task.strip():
            continue
        found.setdefault(worker.name, worker)
    return list(found.values())


def segment_worker_key(seg):
    """Match named workers across sources; unnamed runs retain their store identity."""
    return getattr(seg, "runner", "") or (seg.issue_key, seg.stage, seg.attempt)


def source_worker_tasks(sources: board_sources.Sources, tasks: list[Task]) -> set[Task]:
    """Match each cached worker to the same task its Workers-page row would use."""
    by_name = {t.name.strip().casefold(): t for t in tasks if t.name.strip()}
    row_of = launcher(tasks)
    found = set()
    for worker in source_workers(sources):
        task = by_name.get(worker.task.strip().casefold()) or row_of(worker.task)[0]
        if task is not None:
            found.add(task)
    return found


def live_worker_keys(sources: board_sources.Sources, segments) -> set[str | tuple[str, str, int]]:
    """Combine cached workers and open store segments using the Workers page's runner identity."""
    names: set[str | tuple[str, str, int]] = {worker.name for worker in source_workers(sources)}
    names.update(segment_worker_key(seg) for seg in segments if seg.ended_at is None)
    return names


def change_issue_keys(change: board_sources.Change) -> set[str]:
    """Only normalized closing references identify issues in a local change cache."""
    return {ref for ref in change.issues if "://" not in ref}


def task_changes(task: Task, sources: board_sources.Sources) -> tuple[board_sources.Change, ...]:
    """The changes naming the task's issue: the open ones, or else the merged ones."""
    keys = set(lookup_keys((task.issue,), sources.home))
    named = [c for c in sources.changes.values() if keys and (keys & change_issue_keys(c)) and c.state != "closed"]
    return tuple(c for c in named if c.state == "open") or tuple(named)


def effective_stage(task: Task, sources: board_sources.Sources, lanes: dict) -> str:
    """A task's real stage for drawing, drift and hold: the tracker's own cell, unless its change already
    merged and the tracker hasn't caught up — then the lane's last stage. The forge already answered the only
    question a stale `merge` cell was tracking; a stalled coordinator can otherwise leave it stuck for hours (#240)."""
    stage = task.stage.strip()
    changes = task_changes(task, sources)
    lane = lanes.get(task.lane.strip())
    if changes and changes[0].state == "merged" and lane and lane.stages:
        return lane.stages[-1]
    return stage


def hold_causes(tasks: list[Task], kanban: Kanban | None, sources: board_sources.Sources, lanes: dict,
                pending_refs: Container[str]) -> dict[Task, str]:
    """Each held task to its `hold_cause` at its effective stage, decided once: the BUILD flag, the NEEDS INPUT tile and
    the Flow note all read it (#228)."""
    workers = worker_names(sources)
    return {t: cause for t in tasks
            if (cause := hold_cause(t, kanban, effective_stage(t, sources, lanes), workers=workers,
                                    pending_refs=pending_refs, home=sources.home, lane=lanes.get(t.lane.strip())))}


def current_tasks(tasks: list[Task], rows: list[Task], home) -> dict[str, Task | None]:
    """Resolve a snapshot's names/items to today's tasks. Issue identity wins over a reused name.
    Unmatched aliases stay explicitly unmatched, so that day's moves cannot fall back to today's names."""
    by_name = {(t.name.strip() or t.item.strip()).casefold(): t for t in tasks if t.name.strip() or t.item.strip()}
    by_issue = {issue_key(t.issue, home): t for t in tasks if t.issue.strip() and not t.workflow}

    def today(t: Task) -> Task | None:
        key = issue_key(t.issue, home) if t.issue.strip() and not t.workflow else None
        if key and key in by_issue:
            return by_issue[key]
        named = by_name.get((t.name.strip() or t.item.strip()).casefold())
        if key and named and named.issue.strip() and not named.workflow and issue_key(named.issue, home) != key:
            return None
        return named

    return {k: today(t) for t in rows for k in (t.item.strip().casefold(), t.name.strip().casefold()) if k}


def _current_tasks(tasks: list[Task], earlier: list[tuple[date, list[Task]]], home) -> dict[date | None, dict[str, Task | None]]:
    """Keep each day's aliases separate: a rename and reuse must not steal each other's moves."""
    return {None: current_tasks(tasks, tasks, home),
            **{day: current_tasks(tasks, rows, home) for day, rows in earlier}}


def looping(tasks: list[Task], moves: list[Move], lanes: dict, home, earlier: list[tuple[date, list[Task]]] = ()) -> set[Task]:
    """Tasks with at least two entries into their lane's fix stage. Repeated logs at the same stage are one
    entry; launcher lifecycle lines are not lane moves. Earlier row names resolve as they do for hold_began."""
    find = _current_tasks(tasks, earlier, home)
    last, visits = {}, {}
    for move in sorted(moves, key=lambda m: m.at):
        task = find.get(move.at.date(), find[None]).get(move.name.strip().casefold())
        if task is None or move.launch:
            continue
        lane = lanes.get(task.lane.strip())
        if task.kind == "done" or (lane and lane.stages and task.stage.strip() == lane.stages[-1]):
            continue
        if lane and "fix" in lane.stages and move.stage == "fix" and last.get(task) != "fix":
            visits[task] = visits.get(task, 0) + 1
        last[task] = move.stage
    return {t for t, count in visits.items() if count >= 2}


def hold_began(held: dict[Task, str], moves: list[Move], pending_refs: Mapping[str, datetime | None], home,
               tasks: list[Task], earlier: list[tuple[date, list[Task]]] = ()) -> dict[Task, datetime | None]:
    """When each of the `held` tasks' holds began (#229): a decision when the first decision holding its issue was asked
    (`pending_refs`, decision_page.held_refs); a gate stage at the task's last move into it, the moves read as flow_chart
    reads them (by name or item in any case, a launcher line only until the task's first `stage:` move); a person's
    wait, which no line records, None. `earlier` holds dated task snapshots; a move belongs to the issue that
    owned its name on that day."""
    if earlier and isinstance(earlier[0], Task):
        earlier = [(None, list(earlier))]
    find = _current_tasks(tasks, [e for e in earlier if e[0] is not None], home)
    if any(e[0] is None for e in earlier):
        for _, rows in (e for e in earlier if e[0] is None):
            find[None] = {**current_tasks(tasks, rows, home), **{k: v for k, v in find[None].items() if v is not None}}
    launchers = {None: launcher(tasks), **{day: launcher(rows) for day, rows in earlier if day is not None}}
    staged, into = set(), {}
    for m in sorted(moves, key=lambda m: m.at):
        aliases = find.get(m.at.date(), find[None])
        key = m.name.strip().casefold()
        t = aliases.get(key)
        if key not in aliases and m.launch:
            row, _ = launchers.get(m.at.date(), launchers[None])(m.name)
            if row:
                t = aliases.get(row.item.strip().casefold())
        if t is None or (m.launch and t in staged):
            continue
        staged |= set() if m.launch else {t}
        into[t, m.stage] = m.at
    return {t: pending_refs.get(issue_key(t.issue, home)) if cause == "decision" else into.get((t, cause))
            for t, cause in held.items()}


def went_held(held: dict[Task, str], began: dict[Task, datetime | None], since: datetime) -> int:
    """How many of the `held` tasks' holds began after `since` (#229). A hold whose start is not known is not counted:
    it would claim a change nobody can date. The Log keeps minutes, so a gate hold logged in `since`'s own minute
    counts; a decision's asked time keeps seconds, so it is compared as it is."""
    minute = since.replace(second=0, microsecond=0)
    return sum(1 for t, cause in held.items()
               if (at := began.get(t)) is not None and at >= (since if cause == "decision" else minute))


def held_names(held: dict[Task, str]) -> dict[str, str]:
    """`hold_causes` by task name (or item when nameless), as flow_chart.build reads it."""
    return {(t.name.strip() or t.item.strip()): cause for t, cause in held.items() if t.name.strip() or t.item.strip()}


def forge_ends(tasks: list[Task], sources: board_sources.Sources, zone: ZoneInfo) -> dict[str, tuple[datetime, str]]:
    """Each task's name to what ends its Flow row, in `zone`: its change's latest forge merge time and "merged", or,
    failing that, its closed issue's close time and "closed". A merge wins. The board and the task page both end a
    row here (#200, #220)."""
    out = {}
    for t in tasks:
        name = t.name.strip()
        if not name:
            continue
        if merges := [c.merged_at for c in task_changes(t, sources) if c.state == "merged" and c.merged_at]:
            out[name] = (max(merges).astimezone(zone), "merged")
        elif (issue := next((sources.issues[k] for k in lookup_keys((t.issue,), sources.home)
                             if k in sources.issues), None)) and issue.state == "closed" and issue.closed_at:
            out[name] = (issue.closed_at.astimezone(zone), "closed")
    return out


def epic_risk(tasks: list[Task], sources: board_sources.Sources, ends: Mapping[str, datetime], zone: ZoneInfo,
              now: datetime) -> tuple[int, int]:
    """(#230) (at risk, late): the milestones of the cached issues, each once by its URL. Late: open, past its due date
    in `zone`, with open issues. At risk: open, not late, and the latest of `ends` (the board's estimate, by item) among
    its open tasks on open issues falls after its due date in `zone`. A milestone with no due date, or no estimated open
    task, is neither."""
    today = now.astimezone(zone).date()
    milestones = {i.milestone.url: i.milestone for i in sources.issues.values() if i.milestone}
    forecast: dict[str, datetime] = {}
    for t in tasks:
        issue = sources.issues.get(issue_key(t.issue, sources.home)) if t.issue.strip() else None
        if t.kind != "done" and t.item in ends and issue and issue.state == "open" and issue.milestone:
            url = issue.milestone.url
            forecast[url] = max(forecast.get(url, ends[t.item]), ends[t.item])
    late = {u for u, m in milestones.items() if m.state == "open" and m.due and m.due < today and m.open_issues}
    at_risk = {u for u, m in milestones.items() if m.state == "open" and m.due and u not in late
               and u in forecast and forecast[u].astimezone(zone).date() > m.due}
    return len(at_risk), len(late)


def drifts(task: Task, kanban: Kanban | None, sources: board_sources.Sources, stage: str | None = None) -> bool:
    """The forge's labels disagree with the task's stage, by the same check the audit runs (kanban.drift).
    `stage` overrides the tracker's own cell — pass `effective_stage()` so a merged change's card is judged at
    the stage it now draws in, not the one it's stuck reading (#240)."""
    issue = sources.issues.get(issue_key(task.issue, sources.home)) if task.issue.strip() else None
    stage = task.stage.strip() if stage is None else stage
    # ponytail: labels only; a GitHub Project's Status is not in the sources, so a project board never drifts here.
    if kanban is None or kanban.github_project or issue is None or not stage or task.kind == "done":
        return False
    if stage in kanban.terminal_stages:  # a merged change: the forge already answered, mark or not (#240)
        return False
    return bool(kanban_tool.drift(kanban, issue.labels, stage, allow_extra_hold=task.kind == "waiting"))


def ready_tasks(tracker_text: str, max_concurrency: int = 1) -> list[Task]:
    """The dispatcher's open, unassigned candidates, bounded by free slots."""
    parsed = tracker.parse_tracker(tracker_text)
    tasks = parsed.tasks

    running = [t for t in tasks if t.kind == "running"]
    free_slots = max(0, max_concurrency - len(running))
    if free_slots <= 0:
        return []

    ready: list[tracker.Task] = []

    for t in tasks:
        if t.kind != "open" or t.owner.strip() != "unassigned":
            continue
        # standing_for unmarks the item; a bold name (`**impl03**: standing`) is still a placeholder when unassigned.
        if t.workflow or t.standing or tracker.STANDING_ITEM.match(tracker._unmark(t.item).strip()) or tracker.STANDING_NAME.match(t.name.strip()):
            continue
        if t.stage.strip().startswith("hold"):
            continue

        ready.append(t)
        if len(ready) >= free_slots:
            break

    return ready


def needs_input(tracker_text: str) -> int:
    """Today's human-review ends in the tracker log."""
    return sum(1 for line in section(tracker_text, "## Log")
               if (m := tracker_log.ENDED.match(line.strip())) and m[4] == tracker_log.HUMAN_REVIEW)


def workers_need_input(tracker_text: str = "", items=None, segments=(), resumes: dict | None = None) -> int:
    """Workers badge and tile: current store waits, or tracker review ends without a store.

    Store snapshots are authoritative; tracker ends are history and must not add another wait.
    """
    return (needs_input(tracker_text) if items is None else
            len(decision_actions.waiting_workers(items, segments, resumes)))


def tab_counts(tracker_text: str, cfg, now: datetime, lanes: dict, kanban: Kanban | None,
               sources: board_sources.Sources, pending_refs: Mapping[str, datetime | None],
               looked: datetime | None = None, earlier=(), slots: int = 1, *, held=None, ends=None,
               tracker_day: date | None = None) -> dict:
    """The pages' lead counts from the same tracker, hold rules, forecasts and cached forge facts as the board.
    `earlier` is (day, tracker text) before this tracker; the board can supply its already computed holds and ends."""
    tasks = [t for t in tracker.parse_tracker(tracker_text).tasks if not t.standing]
    held = hold_causes(tasks, kanban, sources, lanes, pending_refs) if held is None else held
    since = looked or datetime.combine(now.date(), time(), cfg.zone)
    hold_earlier = [(d, text) for d, text in earlier if d >= since.astimezone(cfg.zone).date()]
    moves = [m for day, text in [*hold_earlier, (tracker_day or now.date(), tracker_text)]
             for m in tracker_log.moves(text, day, cfg.zone)]
    began = hold_began(held, moves, pending_refs, sources.home, tasks,
                       [(d, tracker.parse_tracker(text).tasks) for d, text in hold_earlier])
    shown = since.astimezone(cfg.zone)
    if ends is None:
        active = [t for t in tasks if t.kind != "done"]
        est = estimates(active, cfg, now, history(tasks, cfg, now))
        ends = {t.item: end[0] for t in active
                if (end := task_end(t, cfg, now, now, est))[1] in ("due", "derived")}
    return {"workers": workers_need_input(tracker_text),
            "dispatch": len(ready_tasks(tracker_text, slots)),
            "since": (went_held(held, began, since), f"{shown:%H:%M}" if shown.date() == now.date() else f"{shown:%a %H:%M}"),
            "epics": epic_risk(tasks, sources, ends, cfg.zone, now)}


@dataclass(frozen=True)
class PageData:
    """Detached rows for all page presenters; never retains a store connection."""
    work_items: tuple
    stage_segments: tuple
    board_decisions: tuple
    stage_reports: tuple
    resumes: dict[str, Any]
    issue_logs: tuple = ()
    engine_logs: tuple = ()
    decision_rows: tuple = ()  # every decision row, oldest first: a decision page's history and address (#945)

    def items(self):
        return self.work_items

    def item(self, key):
        return next((i for i in self.work_items if i.issue_key == key), None)

    def segments(self):
        return self.stage_segments

    def decisions(self):
        return self.board_decisions

    def reports(self, key, *, stage, attempt):
        return [r for r in self.stage_reports if (r.issue_key, r.stage, r.attempt) == (key, stage, attempt)]

    def log(self, key=None):
        return [entry for entry in self.issue_logs if key is None or entry.issue_key == key]

    def resume(self, key):
        return self.resumes.get(key)

    def exhaustion(self, key) -> RetryExhaustion | None:
        """Current retry exhaustion, unless the issue is closed or a live run supersedes it."""
        item = self.item(key)
        if item is None or item.forge_state == "closed":
            return None
        segs = [s for s in self.stage_segments if s.issue_key == key]
        return retry_exhaustion(self.resumes.get(key), segs, [e for e in self.issue_logs if e.issue_key == key])


def page_data(store) -> PageData:
    """Read the store through the use case once, before handing rows to presenters."""
    items = tuple(store.items())
    rows = tuple(store.decisions())
    # Decision rows are append-only; presenters see each decision's latest row only, a decision page all of them.
    return PageData(items, tuple(store.segments()), tuple(decision_actions.current_rows(rows)), tuple(store.reports()),
                    {i.issue_key: store.resume(i.issue_key) for i in items}, tuple(store.log()), tuple(store.engine_log()),
                    rows)

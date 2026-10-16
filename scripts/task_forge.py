"""What the forge's cached sources say about the tracker's tasks: the changes naming a task's issue, the stage a
merged change has moved it to, whether the issue's labels drift from that stage, and when its Flow row ends. Beside
them, who the workers are and whether a stage holds. The board and the task page read these; nothing here draws.
"""

from __future__ import annotations

from collections.abc import Container, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

import board_sources
import kanban as kanban_tool
from settings import Kanban
from tracker import Task, issue_key, launcher
from tracker_log import Move


def hold_cause(task: Task, kanban: Kanban | None, stage: str | None, *, workers: set[str], pending_refs: Container[str],
               home, lane) -> str:
    """What holds the task, or "" when nothing does, at `stage` (its own cell when None): `decision` when a pending
    decision holds its issue (`pending_refs`, keyed as issue_key keys it under `home`), else the stage when [kanban] holds it there, else the person a waiting task waits on.
    The one place a hold is decided, so the BUILD flag, the NEEDS INPUT tile and the Flow note agree (#228). Nothing
    holds a finished task: done, or at its `lane`'s last stage, where a merged change puts it (#240)."""
    stage = task.stage.strip() if stage is None else stage
    if task.kind == "done" or (lane and lane.stages and stage == lane.stages[-1]):
        return ""
    if task.issue.strip() and issue_key(task.issue, home) in pending_refs:
        return "decision"
    if kanban is not None and kanban.holds(stage):
        return stage
    if task.kind == "waiting" and task.shown_owner and task.shown_owner not in workers:
        return task.shown_owner
    return ""


def worker_names(sources: board_sources.Sources) -> set[str]:
    return {w.name for w in sources.workers}


def task_changes(task: Task, sources: board_sources.Sources) -> tuple[board_sources.Change, ...]:
    """The changes naming the task's issue: the open ones, or else the merged ones."""
    key = issue_key(task.issue, sources.home) if task.issue.strip() else None
    named = [c for c in sources.changes.values() if key and key in c.issues and c.state != "closed"]
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


def _current_tasks(tasks: list[Task], earlier: list[Task], home) -> dict[str, Task]:
    """Names and items in current and earlier rows, resolved to today's task (renames keep their issue)."""
    by_name = {t.name.strip().casefold(): t for t in tasks if t.name.strip()}
    by_issue = {issue_key(t.issue, home): t for t in tasks if t.issue.strip()}

    def today(t: Task) -> Task | None:
        return by_name.get(t.name.strip().casefold()) or (by_issue.get(issue_key(t.issue, home)) if t.issue.strip() else None)

    return {**{k: cur for t in earlier for k in (t.item.strip().casefold(), t.name.strip().casefold())
               if k and (cur := today(t))},
            **{t.item.strip().casefold(): t for t in tasks if t.item.strip()}, **by_name}


def looping(tasks: list[Task], moves: list[Move], lanes: dict, home, earlier: list[Task] = ()) -> set[Task]:
    """Tasks with at least two entries into their lane's fix stage. Repeated logs at the same stage are one
    entry; launcher lifecycle lines are not lane moves. Earlier row names resolve as they do for hold_began."""
    find = _current_tasks(tasks, earlier, home)
    last, visits = {}, {}
    for move in sorted(moves, key=lambda m: m.at):
        task = find.get(move.name.strip().casefold())
        if task is None or move.launch:
            continue
        lane = lanes.get(task.lane.strip())
        if lane and "fix" in lane.stages and move.stage == "fix" and last.get(task) != "fix":
            visits[task] = visits.get(task, 0) + 1
        last[task] = move.stage
    return {t for t, count in visits.items() if count >= 2}


def hold_began(held: dict[Task, str], moves: list[Move], pending_refs: Mapping[str, datetime | None], home,
               tasks: list[Task], earlier: list[Task] = ()) -> dict[Task, datetime | None]:
    """When each of the `held` tasks' holds began (#229): a decision when the first decision holding its issue was asked
    (`pending_refs`, decision_page.held_refs); a gate stage at the task's last move into it, the moves read as flow_chart
    reads them (by name or item in any case, a launcher line only until the task's first `stage:` move); a person's
    wait, which no line records, None. A move may name an `earlier` tracker's row: it is today's task of that row's
    name, or, renamed, of its issue."""
    find = _current_tasks(tasks, earlier, home)
    row_of = launcher(tasks)
    staged, into = set(), {}
    for m in sorted(moves, key=lambda m: m.at):
        t = find.get(m.name.strip().casefold()) or (row_of(m.name)[0] if m.launch else None)
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
    """`hold_causes` by task name, as flow_chart.build reads it. A nameless task has no name to hold it by (as forge_ends)."""
    return {t.name.strip(): cause for t, cause in held.items() if t.name.strip()}


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
        elif (issue := sources.issues.get(issue_key(t.issue, sources.home)) if t.issue.strip() else None) and issue.state == "closed" and issue.closed_at:
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

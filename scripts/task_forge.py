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
from tracker_log import Move, review_completions


STAGE_REVIEW = "stage_review"
REVIEW_LABEL = "needs your review"


def _review_hold(task: Task, stage: str, *, workers: set[str], review_stages: Container[str],
                 completed_review: bool) -> bool:
    return bool(completed_review and task.kind == "waiting" and stage in review_stages
                and task.shown_owner and task.shown_owner not in workers)


def hold_cause(task: Task, kanban: Kanban | None, stage: str | None, *, workers: set[str], pending_refs: Container[str],
               home, lane, review_stages: Container[str] = frozenset(), completed_review: bool = False) -> str:
    """Return "" for done or forge-advanced tasks; "decision" for pending_refs; STAGE_REVIEW for a completed
    waiting stage in review_stages; stage for a kanban gate; task.shown_owner for a human wait, else "".
    completed_review is one-shot launcher evidence, never inferred from a waiting row or the engine store.
    A terminal stage ends the task unless its own completed result still requires review (#130, #240).
    """
    stage = task.stage.strip() if stage is None else stage
    if task.kind == "done" or stage != task.stage.strip():
        return ""
    review_hold = _review_hold(task, stage, workers=workers, review_stages=review_stages,
                               completed_review=completed_review)
    if lane and lane.stages and stage == lane.stages[-1] and not review_hold:
        return ""
    if task.issue.strip() and issue_key(task.issue, home) in pending_refs:
        return "decision"
    if review_hold:
        return STAGE_REVIEW
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


def canonical_names(tasks: list[Task], earlier: list[Task] = (), home=None) -> dict[str, str]:
    """Map a task's name, item and earlier names of the same issue to today's name."""
    by_name = {t.name.strip().casefold(): t for t in tasks if t.name.strip()}
    by_issue = {issue_key(t.issue, home): t for t in tasks if t.issue.strip()}

    def today(t: Task) -> Task | None:
        return by_name.get(t.name.strip().casefold()) or (by_issue.get(issue_key(t.issue, home)) if t.issue.strip() else None)

    out: dict[str, str] = {}

    def bind(label: str, task: Task) -> None:
        folded = label.strip().casefold()
        canon = (task.name or task.item).strip().casefold()
        if folded and canon:
            out[folded] = canon

    for t in tasks:
        bind(t.name, t)
        bind(t.item, t)
    for t in earlier:
        if cur := today(t):
            bind(t.name, cur)
            bind(t.item, cur)
    return out


def hold_causes(tasks: list[Task], kanban: Kanban | None, sources: board_sources.Sources, lanes: dict,
                pending_refs: Container[str], review_stages: Container[str] = frozenset(), *, moves: list[Move] = (),
                earlier: list[Task] = ()) -> dict[Task, str]:
    """Each held task to its `hold_cause` at its effective stage, decided once: the BUILD flag, the NEEDS INPUT tile and
    the Flow note all read it. review_stages require completed one-shot launcher moves (#228, #130). An engine
    pending_review lives in the store and is not a board hold."""
    workers = worker_names(sources)
    aliases = canonical_names(tasks, earlier, sources.home)
    completed = review_completions(moves, aliases)
    return {t: cause for t in tasks
            if (cause := hold_cause(t, kanban, effective_stage(t, sources, lanes), workers=workers,
                                    pending_refs=pending_refs, home=sources.home, lane=lanes.get(t.lane.strip()),
                                    review_stages=review_stages,
                                    completed_review=(completed.get(aliases.get(t.name.strip().casefold(),
                                                                                t.name.strip().casefold()), (None,))[0]
                                                      == t.stage.strip())))}


def hold_began(held: dict[Task, str], moves: list[Move], pending_refs: Mapping[str, datetime | None], home,
               tasks: list[Task], earlier: list[Task] = ()) -> dict[Task, datetime | None]:
    """When each of the `held` tasks' holds began (#229): a decision when the first decision holding its issue was asked
    (`pending_refs`, decision_page.held_refs); a gate stage at the task's last move into it, the moves read as flow_chart
    reads them (by name or item in any case, a launcher line only until the task's first `stage:` move); a person's
    wait, which no line records, None. STAGE_REVIEW begins at the launcher-confirmed completion. A move may name an
    `earlier` tracker's row: it is today's task of that row's name, or, renamed, of its issue."""
    by_name = {t.name.strip().casefold(): t for t in tasks if t.name.strip()}
    by_issue = {issue_key(t.issue, home): t for t in tasks if t.issue.strip()}

    def today(t: Task) -> Task | None:
        return by_name.get(t.name.strip().casefold()) or (by_issue.get(issue_key(t.issue, home)) if t.issue.strip() else None)

    find = {**{k: cur for t in earlier for k in (t.item.strip().casefold(), t.name.strip().casefold())
               if k and (cur := today(t))},
            **{t.item.strip().casefold(): t for t in tasks if t.item.strip()}, **by_name}
    row_of = launcher(tasks)
    staged, into = set(), {}
    for m in sorted(moves, key=lambda m: m.at):
        t = find.get(m.name.strip().casefold()) or (row_of(m.name)[0] if m.launch else None)
        if t is None or (m.launch and t in staged):
            continue
        staged |= set() if m.launch else {t}
        into[t, m.stage] = m.at
    aliases = canonical_names(tasks, earlier, home)
    completed = {t: at for name, (stage, at) in review_completions(moves, aliases).items()
                 if (t := find.get(name)) is not None and t.stage.strip() == stage}
    return {t: pending_refs.get(issue_key(t.issue, home)) if cause == "decision" else
            completed.get(t) if cause == STAGE_REVIEW else into.get((t, cause))
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
    return {t.name.strip(): REVIEW_LABEL if cause == STAGE_REVIEW else cause
            for t, cause in held.items() if t.name.strip()}


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

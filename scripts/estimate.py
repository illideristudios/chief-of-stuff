"""The board's planning model: the deadline that governs a task, where each owner's queue lands by it, and the mean
time closed tasks of each size took. It draws nothing; render_board draws the Flow forecast
from it and prints its counts in the summary line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from clock import DATE, done_clock as _done_clock, dur as _dur, hhmm as _hhmm
from tracker import NOBODY, UNASSIGNED, Task, bare_name as _bare_name, resolve_due as _resolve_due
from workspace import Config, Deadline

SIZES = ("S", "M", "L", "XL")
ALL_SIZES = "all"
NO_ESTIMATE = "no estimate"
ORPHANED = "orphaned"


@dataclass(frozen=True)
class Bar:
    item: str
    owner: str
    name: str
    kind: str
    start: datetime
    end: datetime
    start_src: str
    end_src: str
    label: str | None
    est: "Estimate | None" = None
    size: str = ""


def nearest_deadline(cfg: Config, now: datetime) -> Deadline:
    """The first deadline at or after `now`, else today's end of day.

    The clock and `data-deadline` use it. A deadline that has already passed is not a countdown;
    end of day is 23:59 in the workspace zone, including when the configured list is empty.
    """
    for d in cfg.deadlines:
        if d.at >= now:
            return d
    return _end_of_day(cfg, now)


def _end_of_day(cfg: Config, now: datetime) -> Deadline:
    return Deadline("end of day", datetime.combine(now.date(), time(23, 59), tzinfo=cfg.zone))


def governing_deadline(cfg: Config, now: datetime) -> Deadline:
    """The nearest deadline ahead that unnamed tasks fall to, else end of day.

    A task that names no deadline is drawn or estimated toward this one. A deadline marked `named tasks
    only` is skipped: after Final the nearest deadline is an exam (finding 73), and nothing on the Tasks
    table is due to it unless its `due` cell says so.
    """
    for d in cfg.deadlines:
        if d.at >= now and d.scope == "all":
            return d
    return _end_of_day(cfg, now)


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


@dataclass(frozen=True)
class Estimate:
    """Where task i of an owner's N lands.

    Two bases. `history`: the queue drains at the mean elapsed time of closed tasks of each size (Zach,
    2026-09-18: "generate an average amount of time spent per t-shirt sized puzzle ... use that"). `queue`:
    no task has closed with a range yet, so the queue drains evenly by the horizon — the 0.10.0 model,
    which claims nothing about effort. The label says which, so a reader can tell a measurement from a share.
    """

    end: datetime
    i: int
    n: int
    of: str
    slot: timedelta
    size: str = ""
    basis: str = "queue"
    count: int = 0
    borrowed: bool = False

    @property
    def label(self) -> str:
        if self.basis == "history":
            return f"est. {self.size or '?'} ~{_dur(self.slot)}"
        return f"est. {self.i}/{self.n} → {self.of}"

    def title(self, owner: str) -> str:
        if self.basis == "history":
            if not self.size:
                rests = f"unsized: the mean of all {self.count} tasks closed with a range, {_dur(self.slot)}"
            elif self.borrowed:
                rests = f"size {self.size}: no {self.size} task has closed with a range, so the mean of all {self.count} closed tasks stands in, {_dur(self.slot)}"
            else:
                rests = f"size {self.size}: mean of {self.count} {self.size} tasks closed with a range, {_dur(self.slot)} each"
            return (f"{self.label}: {rests}; {_ordinal(self.i)} of {self.n} in {owner}'s queue, running first, then oldest first. "
                    "Set a due to override.")
        return (f"{self.label}: where this task lands if {owner}'s queue of {self.n} drains evenly by {self.of}, "
                "running first, then oldest first. Not a claim about effort; set a due to override.")


def _owner_key(owner: str) -> str:
    """The queue an owner cell names, or "" when it names nobody: blank, `unassigned`, `—`, `nobody`."""
    key = _bare_name(owner)
    if not key or NOBODY.match(owner.strip()) or UNASSIGNED.match(key):
        return ""
    return key


def horizon(cfg: Config, now: datetime) -> tuple[datetime, str]:
    """The nearest governing deadline still ahead, or midnight once none is."""
    d = governing_deadline(cfg, now)
    if d.at > now and d.name != "end of day":
        return d.at, d.name
    return datetime.combine(now.date(), time(0, 0), tzinfo=cfg.zone) + timedelta(days=1), "end of day"


def estimates(active: list[Task], cfg: Config, now: datetime, hist: dict[str, tuple[timedelta, int]] | None = None) -> dict[str, Estimate]:
    """A derived end for every owned task with no `due`, keyed by item. Nothing here is written to the tracker.

    Per owner, the active tasks that are blank or due by the horizon form a queue of N, running first, then
    oldest first. With `hist` (see `history()`), a cursor starts at now and each task takes the mean elapsed
    time of closed tasks of its size — a running task from its own start, never ending before now; a task
    with a `due` takes its time from the queue and keeps its due. Without history no task has a duration to
    learn from, and this reads none — not the item text (its length tracks age, not work left), not the
    Log: task i ends at `H - (N-i)/N x (H - now)`, so task N is the horizon instant, and the label says so.
    Unowned tasks get nothing: there is no queue to place them in, and the deadline they already draw to
    is the honest end. A gone owner — one an `orphaned` task names — is unowned for all its tasks.
    """
    h, of = horizon(cfg, now)
    r = h - now
    left = {_bare_name(task.owner) for task in active if task.kind == ORPHANED} - {""}
    queues: dict[str, list[tuple[tuple, Task]]] = {}
    for idx, task in enumerate(active):
        if task.kind in ("done", ORPHANED):
            continue
        key = _owner_key(task.owner)
        if not key or key in left:
            continue
        due = _resolve_due(task.due, cfg, now.date())
        if due is not None and due > h:
            continue
        m = DATE.match(task.since.strip())
        try:
            since = date(int(m[1]), int(m[2]), int(m[3])) if m else now.date()
        except ValueError:  # 2026-02-30
            since = now.date()
        queues.setdefault(key, []).append(((task.kind != "running", since, idx), task))
    out: dict[str, Estimate] = {}
    for entries in queues.values():
        entries.sort(key=lambda e: e[0])
        n = len(entries)
        cursor = now
        for i, (_, task) in enumerate(entries, 1):
            if hist:
                size = task.size.strip().upper()
                borrowed = bool(size) and size not in hist
                mean, count = hist[size] if size and size in hist else hist[ALL_SIZES]
                start = _hhmm(task.state_time, now.date(), cfg.zone) if task.kind == "running" and task.state_time else None
                end = max(now, start + mean) if start else cursor + mean
                cursor = max(cursor, end)
                if not task.due.strip():
                    out[task.item] = Estimate(end, i, n, of, mean, size, "history", count, borrowed)
            elif not task.due.strip():
                out[task.item] = Estimate(h - r * ((n - i) / n), i, n, of, r / n)
    return out


def history(tasks: list[Task], cfg: Config, now: datetime) -> dict[str, tuple[timedelta, int]]:
    """Mean elapsed time per size over done tasks that kept their running start, plus an `all` row.

    Only `done HH:MM–HH:MM` counts: `since` is a date and `done HH:MM` alone has no start (finding 70), so
    a task closed without the range records nothing and is left out rather than guessed at. A range that
    runs backwards crossed midnight and is dropped too. Empty when no task has a range, and the estimator
    then falls back to the queue-drain of 0.10.0, labelled as such.
    """
    today, zone = now.date(), cfg.zone
    spans: dict[str, list[timedelta]] = {}
    for task in tasks:
        if not (r := task.ran):
            continue
        a, b = _hhmm(r[0], today, zone), _hhmm(r[1], today, zone)
        if a is None or b is None or b <= a:
            continue
        spans.setdefault(task.size.strip().upper(), []).append(b - a)
    out = {size: (sum(v, timedelta()) / len(v), len(v)) for size, v in spans.items()}
    if out:
        every = [d for v in spans.values() for d in v]
        out[ALL_SIZES] = (sum(every, timedelta()) / len(every), len(every))
    return out


def queue_durations(active: list[Task], hist: dict[str, tuple[timedelta, int]]) -> dict[str, timedelta]:
    """By item, the mean time closed tasks of each task's size took, as `estimates` reads it; empty with no history."""
    if not hist:
        return {}
    return {t.item: (hist[size] if (size := t.size.strip().upper()) and size in hist else hist[ALL_SIZES])[0] for t in active}


def task_end(task: Task, cfg: Config, now: datetime, start: datetime, est: dict[str, Estimate] | None = None) -> tuple[datetime, str, str | None]:
    today = now.date()
    if (due := _resolve_due(task.due, cfg, today)) is not None:
        return due, "due", None
    if task.kind == "done":
        if t := task.state_time:
            return _done_clock(t, now, cfg.zone), "state", None
        return start, "state", "done, no time"
    if est and (e := est.get(task.item)):
        return max(e.end, start), "derived", e.label
    return governing_deadline(cfg, now).at, "deadline", NO_ESTIMATE


def day_bar(task: Task, cfg: Config, now: datetime, est: dict[str, Estimate] | None = None) -> Bar:
    today, zone = now.date(), cfg.zone
    day_start = datetime.combine(today, time(0, 0), tzinfo=zone)
    if task.kind == "running" and (t := task.state_time):
        start, start_src = _hhmm(t, today, zone), "state"
    elif t := _hhmm(task.since, today, zone):
        start, start_src = t, "since"
    else:
        start, start_src = day_start, "carried"
    end, end_src, label = task_end(task, cfg, now, start, est)
    return Bar(task.item, task.owner, task.label, task.kind, start, end, start_src, end_src, label, est.get(task.item) if est and end_src == "derived" else None, task.size.strip().upper())

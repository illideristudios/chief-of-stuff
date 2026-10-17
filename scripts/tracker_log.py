"""The tracker's `## Log` lines that scripts write and the pages read back: a task's stage move, a one-shot's
start, end and relaunch, a run relaunching after its wait ran out, a hold the launcher could not apply, and a
closure the user approved.

`chief-of-stuff log --stage` and the one-shot launcher write them with the formatters here; the Flow charts, the
board's workers, the Workers page and the issue page read them with the patterns here. Both sides live in one
module that draws nothing, side by side; evals/test_tracker_log.py writes and reads back every line kind, so a
reworded formatter fails there until its pattern follows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

# A one-shot's end line says one of these. A relaunch line says RELAUNCHED instead, and the launcher looks for it so
# a task relaunches at most once a day. A quota stop on a model is relaunched once per task and model a day instead,
# and does not use that daily relaunch. That marker is a launcher-written Log line; a worker's reason can never carry it.
COMPLETED = "completed; awaiting integration"
HUMAN_REVIEW = "HUMAN REVIEW NEEDED"
RELAUNCHED = ": relaunch requested for {task} — "
RELAUNCH_STAGE = "relaunch"

STAGE = re.compile(r"^- (\d{1,2}):(\d{2}) stage: (.+) → (\S+)\s*$")
STARTED = re.compile(r"^- (\d{1,2}):(\d{2}) one-shot (\S+) started: .*, task (.+?)\s*$")
ENDED = re.compile(r"^- (\d{1,2}):(\d{2}) one-shot (\S+): (completed; awaiting integration|HUMAN REVIEW NEEDED)\b")
# RELAUNCHED's Log line, grouped as ENDED is.
RELAUNCH = re.compile(r"^- (\d{1,2}):(\d{2}) one-shot (\S+): (relaunch) requested for ")
# A run whose wait ran out relaunches once into its tree (#110); this line is the once-per-row marker the
# second timeout finds, grouped as RELAUNCH is.
TIMED_OUT = re.compile(r"^- (\d{1,2}):(\d{2}) one-shot (\S+): timed out after (\d+) minutes? — running once more, task (.+?)\s*$")
# A started line's runtime and model and its worktree, for the board's workers and the issue page. STARTED reads
# only the worker and the task.
STARTED_DETAIL = re.compile(r"^-\s+(\d{1,2}:\d{2}) one-shot (\S+) started: (.*?), worktree `([^`]*)`, task (.+?)\s*$")
# The launcher's line when the forge refused the review hold, and the coordinator's record of a forge closure
# the user approved. Each names its task last, as a started line does, and matches no pattern above.
HOLD_FAILED = re.compile(r"^- (\d{1,2}):(\d{2}) one-shot (\S+): review hold failed: (.*), task (.+?)\s*$")
APPROVED_CLOSE = re.compile(r"^- (\d{1,2}):(\d{2}) forge closure approved by the user: (.*), task (.+?)\s*$")


def stage_line(at: str, name: str, stage: str) -> str:
    """`chief-of-stuff log --stage`'s line: the task row named `name` moved to `stage` at `at` (HH:MM)."""
    return f"- {at} stage: {name} → {stage}"


def started_line(at: str, name: str, runtime: str, model: str, tree: str, task: str, effort: str = "") -> str:
    """The launcher's line before worker `name` runs: `started: <runtime> <model> effort=<level>, worktree ...`, the model and
    effort words only when set; the effort names itself, so no model is ever read from it. `tree` is how File
    ownership names the tree, worktree `<tree>` (<branch>); the line leaves the branch out."""
    return f"- {at} one-shot {name} started: {' '.join(filter(None, (runtime, model, effort and f'effort={effort}')))}, {tree.split(' (')[0]}, task {task}"


def one_period(reason: str) -> str:
    """`reason` with exactly one final period."""
    return reason if reason.endswith(".") else reason + "."


def ended_line(at: str, name: str, status: str, reason: str, changes: str) -> str:
    """The launcher's line when worker `name` stops `done`, or with any other status for review. `reason` and
    `changes` come already on one line."""
    return (f"- {at} one-shot {name}: {COMPLETED if status == 'done' else HUMAN_REVIEW}"
            f" — {one_period(reason)} Changes: {changes}.")


def relaunch_line(at: str, name: str, task: str, reason: str) -> str:
    """The launcher's line when worker `name` asks for `task` to run again; `reason` comes already on one line."""
    return f"- {at} one-shot {name}{RELAUNCHED.format(task=task)}{one_period(reason)}"


def timed_out_line(at: str, name: str, minutes: int, task: str) -> str:
    """The launcher's line when worker `name`'s wait ran out once and the run relaunches into its tree (#110);
    the line is the once-per-row marker the second timeout finds."""
    return f"- {at} one-shot {name}: timed out after {minutes} minutes — running once more, task {task}"


def hold_failed_line(at: str, name: str, task: str, error: str) -> str:
    """The launcher's line when worker `name`'s review hold could not be applied to `task`'s issue (#566); the
    audit reads it back with HOLD_FAILED and keeps reporting the hold until it lands."""
    return f"- {at} one-shot {name}: review hold failed: {error}, task {task}"


def approved_close_line(at: str, task: str, why: str) -> str:
    """The coordinator's record that the user approved `task` done because its issue closed on the forge (#547);
    the audit does not reopen a task this line names."""
    return f"- {at} forge closure approved by the user: {why}, task {task}"


@dataclass(frozen=True)
class Move:
    at: datetime
    name: str
    stage: str
    launch: bool = False
    line: str = field(default="", compare=False)  # the Log line it was read from
    worker: str = field(default="", compare=False)  # a launcher move's worker


def _log_lines(text: str) -> list[str]:
    """The stripped lines of the tracker's `## Log` section."""
    log = next((part for part in re.split(r"(?m)^## ", text) if part.split("\n", 1)[0].strip() == "Log"), "")
    return [line.strip() for line in log.splitlines()]


def hold_failures(text: str) -> dict[str, str]:
    """Task -> the reason of the last hold failure the Log records for it (#566)."""
    out: dict[str, str] = {}
    for line in _log_lines(text):
        m = HOLD_FAILED.match(line)
        if m:
            out[m[5]] = m[4]
    return out


def approved_closes(text: str) -> set[str]:
    """The tasks whose done state the Log records as a user-approved forge closure (#547)."""
    return {m[4] for line in _log_lines(text) if (m := APPROVED_CLOSE.match(line))}


def moves(text: str, day: date, zone: ZoneInfo) -> list[Move]:
    """The `stage:` and one-shot launcher lines of the tracker's `## Log`, stamped on `day`. A launcher move names
    the task key of its worker's active started line; a relaunch is a stop boundary, not a working stage."""
    out, task_of = [], {}
    for line in _log_lines(text):
        m = STAGE.match(line) or STARTED.match(line) or ENDED.match(line) or RELAUNCH.match(line)
        if not m or int(m[1]) >= 24 or int(m[2]) >= 60:
            continue
        at = datetime.combine(day, time(int(m[1]), int(m[2])), tzinfo=zone)
        if m.re is STAGE:
            out.append(Move(at, m[3].strip(), m[4], line=line))
        elif m.re is STARTED:
            task_of[m[3]] = m[4]
            out.append(Move(at, m[4], "implement", True, line, m[3]))
        elif m[3] in task_of:
            if m.re is RELAUNCH:
                # Take the final full separator: the task itself may contain it.
                suffix = RELAUNCHED.split("{task}", 1)[1]
                head, separator, _ = line[m.end(3):].rpartition(suffix)
                if head + separator != RELAUNCHED.format(task=task_of[m[3]]):
                    continue
            stage = RELAUNCH_STAGE if m.re is RELAUNCH else "pr" if m[4].startswith("completed") else "review"
            out.append(Move(at, task_of.pop(m[3]), stage, True, line, m[3]))
    return out

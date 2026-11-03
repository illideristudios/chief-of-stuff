"""The daily tracker: its Tasks and Sessions tables, the Decisions rows, and the names written in them.

A row is read as written. What the board draws from it is render_board.py's; where the tracker is, workspace.py's.
"""

from __future__ import annotations

import functools
import re
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime

from backlog_ref import WORKFLOW, Backlog, GitHubBacklog, IssueRef, issue_ref
from clock import DATE, HHMM, RAN, hhmm as _hhmm
from md import cells as _cells, is_separator as _is_separator, section as _section, split_row as _split_row, unmark as _unmark
from workspace import Config

TASK_COLS = ("item", "owner", "state", "since", "due", "checklist")
TASK_COLS_SIZED = ("item", "owner", "state", "since", "due", "size", "checklist")
TASK_COLS_NAMED = ("name", "item", "owner", "state", "since", "due", "size", "checklist")
# Keep issue and stage columns in the recoverable task span.
TASK_COLS_ISSUED = ("name", "item", "owner", "state", "since", "due", "size", "issue", "checklist")
TASK_COLS_LANED = ("name", "item", "owner", "state", "since", "due", "size", "lane", "stage", "issue", "checklist")
# Match the widest supported tracker header first.
TASK_HEADERS = (TASK_COLS_LANED, TASK_COLS_ISSUED, TASK_COLS_NAMED, TASK_COLS_SIZED, TASK_COLS)


SHORT_NAME = 48


@dataclass(frozen=True)
class Task:
    item: str
    owner: str
    state: str
    since: str
    due: str
    checklist: str
    warning: str = ""
    size: str = ""
    name: str = ""
    issue: str = ""
    lane: str = ""
    stage: str = ""

    @property
    def needs_issue(self) -> bool:
        """A task not yet done. A done row with no issue was closed before the rule and is left alone."""
        return self.kind != "done" and not self.standing

    @property
    def label(self) -> str:
        """What the task is called. A written `name` is the coordinator's judgement and stands;
        with no name column the board falls back to guessing the name out of the prose, as it always has."""
        return self.name.strip() or short_name(_unmark(self.item))

    @property
    def shown_owner(self) -> str:
        """The owner cell as the board shows it: `unassigned`, the tracker's word for no owner, shows as none (#193)."""
        owner = self.owner.strip()
        return "" if UNASSIGNED.match(owner) else owner

    @property
    def kind(self) -> str:
        return (self.state.split() or ["open"])[0].lower()

    @property
    def state_time(self) -> str | None:
        """The one clock the state carries: the running start, or the done end (a range gives its end)."""
        parts = self.state.split()
        if len(parts) < 2:
            return None
        if HHMM.match(parts[1]):
            return parts[1]
        m = RAN.match(parts[1])
        return m.group(2) if m else None

    @property
    def workflow(self) -> bool:
        """A step the pipeline performs: its issue cell holds the token, so it has no issue to file or look up."""
        return self.issue.strip() == WORKFLOW

    @property
    def standing(self) -> bool:
        """A standing session's placeholder, not a task: `impl02: standing implementer. … wait idle`, or a name
        `impl02 — standing implementer`, owned by that same session. spawn_session.py cannot start a session
        without a row, so a session spawned with no task was given one (design inputs #83, #93, #94)."""
        return self.standing_for(bare_name(self.owner))

    def standing_for(self, session: str | None) -> bool:
        """This row is `session`'s standing placeholder, whoever owns it yet: at dispatch it is still `unassigned`."""
        if not session:
            return False
        heads = (STANDING_ITEM.match(_unmark(self.item).strip()), STANDING_NAME.match(self.name.strip()))
        return any(m and m.group(1).lower() == session.lower() for m in heads)

    @property
    def ran(self) -> tuple[str, str] | None:
        """`done 21:16–22:05` → ("21:16", "22:05"): the running start kept on close. None when it was dropped."""
        parts = self.state.split()
        m = RAN.match(parts[1]) if len(parts) > 1 else None
        return (m.group(1), m.group(2)) if m and self.kind == "done" else None


# What a session reports about itself. `starting`, `ready` and `gone` are the coordinator's to work
# out and never a session's to claim: a session cannot report that it is gone, and `ready for
# decommissioning` is a `doing` value that `:189` says never becomes a state of its own.
SESSION_STATES = ("planning", "working", "waiting", "idle")
# The Tasks rule's own vocabulary, as a whole cell. It is what makes an over-wide row recoverable: every other
# task column is prose, a date or blank, and none of them can be told apart from the item they drifted out of.
# A `done` state may carry the sha that landed it (`done 21:16–22:05 db4aa3b`), which `audit_tasks.py` needs to
# clear a task from `merge-base --is-ancestor` rather than from the owner's tree. Without this the one task
# carrying its own evidence would be the one task `anchor` could not recover from a stray pipe.
# Hex and 7–40 wide, so it stays recognisable ON SIGHT: `done, merged by impl-2` is prose, and admitting prose
# here would let an item cell claim the anchor.
TASK_STATE = re.compile(
    r"(?i)^(?:open|waiting|orphaned|running\s+\d{1,2}:\d{2}"
    r"|done(?:\s+\d{1,2}:\d{2}(?:[\u2013-]\d{1,2}:\d{2})?)?(?:\s+[0-9a-f]{7,40})?)$")
# A standing session's row is not a task (Zach, 2026-09-22 19:28: "we don't finish LANES, we finish TASKS").
STANDING_ITEM = re.compile(r"(?i)^([\w.-]+):\s+standing\b")
STANDING_NAME = re.compile(r"(?i)^([\w.-]+)\s+[\u2014\u2013-]\s+standing\b")
READY = re.compile(r"(?i)\bready for decommissioning\b")

# A task whose state says it is still going while its own item cell shouts DONE disputes itself.
# Nothing else reads a task's state: TASK_STATE anchors a stray pipe (`anchor`) and validates
# nothing, so four of eleven live tasks carried a stale state at 03:56 — two overstating progress,
# two understating it — and no instrument saw one of them. A freshness check would need to know
# about the world; this needs only the row, which already carries its own contradiction.
# Case-sensitive, and the same vocabulary `scripts/migrate_backlog.py` uses: a shouted DONE is a
# claim about this row, `resolved by the release` is an ordinary sentence. Matching case-insensitively
# flagged 17 rows there, mostly the latter. A false flag is noisy and self-clearing; a false clear
# is silent and permanent, so this errs loud.
DONE_WORDS = re.compile(r"\b(?:DONE|FINISHED|LANDED|CLEARED|CANCELLED|SUPERSEDED|RESOLVED|DELIVERED)\b")


def _disputes_itself(state: str, item: str) -> str:
    """A task's state cell read against the task's own prose. Empty when the row agrees with itself."""
    if state.strip().lower().startswith("done"):
        return ""
    said = DONE_WORDS.search(item)
    return f"state {state.strip()!r} but the item says {said.group(0)}" if said else ""


# `Zach — A2, A3, B`: the target, then why. An em dash, an en dash or a hyphen, because three
# different sessions have written this cell and they did not agree.
WAITS = re.compile(r"\s+[—–-]\s+|,\s+")
# `nothing; the stack-default fix landed` is what a live row says when a session waits on no one.
# Drawn literally it is an edge to a node called "nothing".
NOBODY = re.compile(r"(?i)^(?:(?:nothing|none|nobody|n/?a)\b|[-—–]\s*$)")
# The name cell accretes provenance: `update-claude-md-docs (third name, same ref)`.
NAME_TAIL_TIME = re.compile(r"[\s,;–—-]*(?:at\s+)?\d{1,2}:\d{2}\**$")
PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")
SESSION_REF = re.compile(r"\s*\[[0-9a-f]{4,}\]\s*")


def bare_name(name: str) -> str:
    """`demo-fixes [b8fca1] (fix 5)` → `demo-fixes`. Both sides of a join have to strip the same things."""
    return PARENTHETICAL.sub("", SESSION_REF.sub(" ", _unmark(name))).strip().lower()


@dataclass(frozen=True)
class Session:
    ref: str
    name: str
    state: str
    doing: str
    waiting_on: str
    free_at: str
    constraints: str
    children: str
    last_reply: str
    warning: str = ""

    @property
    def kind(self) -> str:
        """What to draw. Derived beats written, because the coordinator knows things the session cannot."""
        if not self.ref.strip():
            return "starting"
        if READY.search(self.doing):
            return "ready"
        state = self.state.strip().lower()
        if state in SESSION_STATES:
            return state
        # Nothing reported and a word nobody knows are two different facts, and every row of the live
        # tracker was the first one — seven-column rows written before the column existed.
        return "unknown" if state else "unreported"

    @property
    def label(self) -> str:
        """The name without the history the cell accretes. The ref is the key; this is for reading."""
        return PARENTHETICAL.sub("", self.name.strip()).strip() or self.name.strip()

    @property
    def waits_on(self) -> str:
        """Who, as an edge. The graph draws the arrow, so the state stays four words wide."""
        cell = self.waiting_on.strip()
        if not cell or NOBODY.match(cell):
            return ""
        return WAITS.split(cell, 1)[0].strip()

    @property
    def waits_for(self) -> str:
        cell = self.waiting_on.strip()
        if not cell or NOBODY.match(cell):
            return ""
        parts = WAITS.split(cell, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @property
    def child_names(self) -> tuple[str, ...]:
        """A subagent is not a peer: it has no ref, answers no poll, and cannot be sent to.

        So children are a cell on their owner's row rather than rows of their own, and what is
        recoverable from `2: rules-audit (working), fixture-sweep (idle)` is names and count.
        """
        body = self.children.strip()
        if not body or body.lower() in ("none", "-", "—"):
            return ()
        body = body.split(":", 1)[1] if re.match(r"^\d+\s*:", body) else body
        names = [re.sub(r"\s*\(.*?\)\s*$", "", part).strip() for part in body.split(",")]
        return tuple(n for n in names if n)


# 7 columns is every tracker written before the state and children columns existed; 9 is after.
# Positional parsing cannot tell `state` from `doing` without knowing which, so the count decides.
SESSION_OLD = ("ref", "name", "doing", "waiting_on", "free_at", "constraints", "last_reply")
SESSION_NEW = ("ref", "name", "state", "doing", "waiting_on", "free_at", "constraints", "children", "last_reply")


def _session(cells: list[str]) -> Session:
    names = SESSION_NEW if len(cells) >= len(SESSION_NEW) else SESSION_OLD
    warning = "" if len(cells) == len(names) else f"{len(cells)} cells, expected {len(names)}"
    fields = dict(zip(names, (cells + [""] * len(names))[: len(names)]))
    session = Session(**{k: fields.get(k, "") for k in SESSION_NEW}, warning=warning)
    state = session.state.strip().lower()
    if state and state not in SESSION_STATES:
        warning = (warning + "; " if warning else "") + f"state {session.state.strip()!r} is not one of {', '.join(SESSION_STATES)}"
        session = Session(**{k: getattr(session, k) for k in SESSION_NEW}, warning=warning)
    return session


@dataclass(frozen=True)
class Tracker:
    board_url: str | None
    tasks: tuple[Task, ...]
    sessions: tuple[Session, ...] = ()


def _whole(text: str) -> bool:
    """A candidate that ends inside a `**` span hands the rest of the item an orphan mark, and the splitter reads it as prose."""
    return text.count("**") % 2 == 0


def short_name(item: str) -> str:
    """The item up to its first ": " (when that leaves a real name), then up to " (" if still long.

    Both cuts are structural: they find where the name ends and the history begins. Neither loses a
    letter of the name it returns. A name with letters missing is not a shorter name, it is a wrong
    one, and the board has no width that a character cut would be measuring — a `<td>` wraps. The
    cut belongs to a caller that really is one line wide; `clip_name` is that, and `audit_tasks.py`
    printing one finding per terminal line is who calls it.
    """
    name = item.strip()
    head = name.split(": ", 1)[0]
    if head != name and len(head) >= 12 and _whole(head):
        # `**Deploy main.** 21:16: laid out the state` splits after the clock, not before it, so the
        # head comes away holding a time the writer did not put in the name. It costs more than it
        # looks: `history_lines` strips the name off the front of the item to get the history, and a
        # name the item does not literally contain matches nothing — so the headline is printed again
        # as the first history line, under a time column already showing the same clock. Only this
        # branch glues one on; a sentence the writer ended with a time is their sentence.
        name = NAME_TAIL_TIME.sub("", head) or head
    if len(name) > SHORT_NAME and " (" in name and _whole(name.split(" (", 1)[0]):
        name = name.split(" (", 1)[0]
    if len(name) > SHORT_NAME:
        clause = _head_clause(name)
        if clause != name and _whole(clause):
            name = clause
    return name


def _head_clause(text: str) -> str:
    """The first sentence, its punctuation kept. Where an item's name ends when nothing else says.

    `_depth0_split` answers the same question and is the right one to ask — the name has to end where
    the history begins, or the cell loses a clause between the two. It hands its parts back stripped,
    though, and `Session TTL fix, orphaned.` without the full stop is not what the writer wrote.
    """
    parts = _depth0_split(text)
    if len(parts) < 2:
        return text
    end = text.find(parts[0])
    if end < 0:
        return text
    end += len(parts[0])
    return text[: end + 1] if text[end : end + 1] in ".;" else text[:end]


def clip_name(item: str, cap: int = SHORT_NAME) -> str:
    """`short_name` for a medium with a real width, capped on a word boundary.

    Every cut stops at a whole number of mark spans: `**22:19: copied**` holds a ": " and a full stop,
    and cutting through it leaves `**` on one side and the rest unbalanced.
    """
    name = short_name(item)
    if len(name) > cap:
        cut = name[:cap]
        if not _whole(cut):
            # Trimming back to a whole number of mark spans empties the cut when the only opening
            # mark is the first character, and a bare `…` is not a short name, it is the loss of one.
            trimmed = cut[: cut.rfind("**")]
            cut = trimmed if trimmed.strip(" ,;:–—-") else _unmark(name)[:cap]
        space = cut.rfind(" ")
        name = (cut[:space] if space > cap // 2 else cut).rstrip(" ,;:–—-") + "…"
    return name


def _depth0_split(text: str) -> list[str]:
    """Split on "; " and ". " outside parentheses and outside a `**bold**` span.

    A mark pair is a span like a parenthesis. A bold clause routinely holds a full stop, and cutting
    through one leaves half a mark on each side — 21 live history bullets read `**…` for that reason.
    An item with an odd number of marks is already unbalanced, and tracking them there would swallow
    every later split, so the tracking is switched off for it.
    """
    parts, depth, start, i, marked = [], 0, 0, 0, False
    spans = text.count("**") % 2 == 0
    while i < len(text):
        if spans and text[i : i + 2] == "**":
            marked = not marked
            i += 2
            continue
        c = text[i]
        depth += c == "("
        depth -= c == ")" and depth > 0
        if depth == 0 and not marked and c in ";." and text[i + 1 : i + 2] == " ":
            parts.append(text[start:i])
            start = i + 2
            i += 1
        i += 1
    parts.append(text[start:])
    return [p.strip().rstrip(".").strip() for p in parts if p.strip().rstrip(".").strip()]


def decision_rows(tracker_text: str) -> list[list[str]]:
    """The Decisions table's rows as (time, item, words), header and separator left out."""
    rows = [_cells(line) for line in _section(tracker_text, "## Decisions") if line.strip().startswith("|")]
    return [(cells + ["", "", ""])[:3] for cells in rows[1:] if not _is_separator(cells)]


def anchor(raw: list[str], cols: tuple[str, ...]) -> list[str] | None:
    """A row wider than its table is one cell that grew a pipe. Re-read it from its state column — the one cell
    of a task row a machine can recognise on sight: `item` absorbs the excess on its left and `checklist` the
    excess on its right, which is where the prose, and so the stray pipe, lives. Exactly one cell may claim the
    anchor; otherwise the row is left as it was written and only the warning speaks."""
    hits = [i for i, c in enumerate(raw) if TASK_STATE.match(c.strip())]
    if len(hits) != 1:
        return None
    s, state = hits[0], cols.index("state")
    if s < state or len(raw) - s < len(cols) - state:
        return None
    head = ["|".join(raw[: s - state + 1])] + raw[s - state + 1 : s]
    tail = raw[s + 1 : s + len(cols) - state - 1] + ["|".join(raw[s + len(cols) - state - 1 :])]
    return head + [raw[s]] + tail


def parse_tracker(text: str) -> Tracker:
    head = re.split(r"(?m)^## ", text, maxsplit=1)[0]
    url = None
    m = re.search(r"(?m)^\s*(?:[-*]\s*)?(?:Coordinator:[^\n]*?)?Board:\s*(\S+)", head)
    if m:
        candidate = m.group(1).rstrip(".,;")
        if candidate and not candidate.startswith("<") and candidate.lower() != "none":
            url = candidate
    tasks: list[Task] = []
    # `## Lanes` is the heading a tracker had before #33 made every row a task.
    rows = [line for line in _section(text, "## Tasks") or _section(text, "## Lanes") if line.strip().startswith("|")]
    # The header decides the width: a six-column table parses as it always has, every task unsized and unnamed.
    header = [c.lower() for c in _cells(rows[0])] if rows else []
    cols = next((h for h in TASK_HEADERS if header == list(h)), TASK_COLS)
    for row in rows[1:]:
        raw = _split_row(row)
        cells = [c.strip() for c in raw]
        if _is_separator(cells):
            continue
        warning = "" if len(cells) == len(cols) else f"{len(cells)} cells, expected {len(cols)}"
        if len(cells) > len(cols):
            fixed = anchor(raw, cols)
            cells = [c.strip() for c in fixed] if fixed else cells
        fields = dict(zip(cols, (cells + [""] * len(cols))[:len(cols)]))
        dispute = _disputes_itself(fields.get("state", ""), fields.get("item", ""))
        if dispute:
            warning = (warning + "; " if warning else "") + dispute
        tasks.append(Task(**fields, warning=warning))
    sessions: list[Session] = []
    for row in [line for line in _section(text, "## Sessions") if line.strip().startswith("|")][1:]:
        cells = _cells(row)
        if _is_separator(cells) or not any(c.strip() for c in cells):
            continue
        sessions.append(_session(cells))
    return Tracker(board_url=url, tasks=tuple(tasks), sessions=tuple(sessions))


def resolve_due(due: str, cfg: Config, today: date) -> datetime | None:
    value = due.strip()
    if not value:
        return None
    if t := _hhmm(value, today, cfg.zone):
        return t
    m = DATE.match(value)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 23), int(m[5] or 59), tzinfo=cfg.zone)
        except ValueError:  # 2026-02-30, 25:00: not a date, so no due
            return None
    for d in cfg.deadlines:
        if d.name.lower() == value.lower():
            return d.at
    return None


UNASSIGNED = re.compile(r"(?i)^unassigned$")


TRAILING_NUMBER = re.compile(r"(\d+)\s*$")


def issue_number(cell: str) -> int | None:
    """An issue URL or issue key's number, including bare #N and canonical host/project#N keys."""
    if ref := issue_ref(cell, None, any_host=True):
        return ref.number
    key, marker, number = cell.strip().rpartition("#")
    return int(number) if marker and number.isdecimal() and "://" not in key else None


def issue_key(cell: str, home: Backlog | GitHubBacklog | None = None) -> str:
    """The key a board source files an issue under (`IssueRef.label`, as board_sources writes it), from an issue cell:
    `#118`, `group/app#118` or the issue's URL. A cell that names no issue, or a bare `#N` with no `home`, keeps `#N`."""
    if ref := issue_ref(cell, home, any_host=True):
        return ref.label(home)
    # A qualified but unresolved identity is never the bare issue in the home project.
    if re.fullmatch(r"[\w.:-]+(?:/[\w.:-]+)+#\d+", cell.strip()):
        return cell.strip()
    m = TRAILING_NUMBER.search(cell)
    return f"#{m[1]}" if m else cell.strip()


def change_keys(text: str, home: Backlog | GitHubBacklog | None = None) -> tuple[str, str]:
    """(issue key, change URL) from one change and its own issue clause; ambiguous references give empty strings.
    A bare issue resolves in the change's project, using the shared issue-reference parser."""
    # ponytail: keyed only when the text names exactly one distinct change; URLs with suffixes (/files, #note),
    # bare !N or #N references, and stacked reviews naming two changes stay unkeyed.
    changes = {}
    for m in re.finditer(r"https://[^\s`<>()]+", text):
        url = m[0].rstrip(".,;:")
        issue_url = re.sub(r"/(?:merge_requests|pull)/(\d+)(/?)$", r"/issues/\1\2", url)
        if issue_url != url and (ref := issue_ref(issue_url, None, any_host=True)):
            changes.setdefault(url.rstrip("/"), (ref, m.end()))
    if len(changes) != 1:
        return "", ""
    change, (project, start) = next(iter(changes.items()))
    issues = {IssueRef(project.repo, int(n), project.host).label(home)
              for n in re.findall(r"(?i)\b(?:issue|close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", text[start:])}
    if len(issues) > 1:
        return "", ""
    return next(iter(issues), ""), change


# A `#N` or `!N` issue or change reference in a one-shot's launch text.
REF = re.compile(r"(?<!\w)[#!]\d+")


def launch_row(text: str, tasks):
    """(row, name) for a one-shot's launch-time task text: the row whose item the text begins with, else the row
    named by a leading "name:", else a grown item beginning with at least 40 characters of launch text,
    else the row whose issue ref it carries (later rows win), and that row's name;
    with no row, None and the text's first line cut to 80 characters. The launch keeps its text; the row's item
    may change after it (#182)."""
    return launcher(tasks)(text)


def launcher(tasks):
    """launch_row over `tasks`, indexed once: items by length, task names (with a trailing ":") by length,
    grown items in sorted order, `#N`/`!N` issues by ref, and each text looked up once (#184). An issue cell of another shape
    (`group/app#118`, a URL) keeps its own search."""
    # ponytail: 40-character grown minimum, prose-derived; persist launch identity if shorter or rewritten items must match.
    items, names, refs, other = {}, {}, {}, []
    for i, t in enumerate(tasks):
        if item := t.item.strip():
            items[item] = (i, t)
        if name := t.name.strip():
            names[name + ":"] = (i, t)  # a launch text naming its task before ":" (#204)
        if (issue := t.issue.strip()) and not t.workflow:
            if REF.fullmatch(issue):
                refs[issue] = (i, t)
            else:
                other.append(((i, t), re.compile(rf"(?<!\w){re.escape(issue)}(?!\d)")))
    lengths = {len(k) for k in items}
    name_lengths = {len(k) for k in names}
    sorted_items = sorted(items)

    def grown(text: str):
        if len(text) >= 40:
            i = bisect_left(sorted_items, text)
            while i < len(sorted_items) and sorted_items[i].startswith(text):
                if re.match(r"\W|$", sorted_items[i][len(text):]):
                    yield items[sorted_items[i]]
                i += 1

    @functools.cache
    def row(text: str):
        text = text.strip()
        first = text.split("\n", 1)[0][:80]
        for rung, hits in enumerate(([items.get(text[:n]) for n in lengths],
                                     [names.get(text[:n]) for n in name_lengths],
                                     grown(text),
                                     [refs.get(r) for r in REF.findall(text)] + [hit for hit, p in other if p.search(text)])):
            if hits := [h for h in hits if h]:
                if rung == 2 and len({t.name.strip() for _, t in hits}) > 1 and len({issue_key(t.issue) for _, t in hits}) > 1:
                    return None, first
                t = max(hits, key=lambda h: h[0])[1]
                return t, t.name.strip() or first
        return None, first
    return row

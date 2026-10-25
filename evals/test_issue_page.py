"""issue_page.py: one tracker issue's page, `/issues/<n>`, from the tracker and the cached sources (Task.dc.html,
"Task page — laptop"). Stage 1: only what the tracker and board_sources already hold.

The module the implementer creates is `scripts/issue_page.py` with

    render(number: int, trackers: list[tuple[date, str]], sources: board_sources.Sources, now: datetime,
           lanes: dict | None = None) -> str | None

`number` is the issue (`109` for the tracker cell `#109`); `trackers` is (day, tracker text), oldest first, the last
being today's, over the days the Flow chart reaches back; `lanes` is the settings' lanes. It returns the page's HTML,
or None when no task in `trackers` names the issue. Sections are `<h2>` headings; the header is one `<header>`.
pages.py serves it at `GET /issues/<n>`, re-rendered on request like the board and decision pages.
"""

import re
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timezone
from html import unescape
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_sources as bs  # noqa: E402
import flow_chart as fc  # noqa: E402
import one_shot  # noqa: E402
import pages as pg  # noqa: E402
import render_board as rb  # noqa: E402
import task_forge  # noqa: E402
from workspace import parse_coordinator  # noqa: E402
import tracker_write as tw  # noqa: E402
from test_flow_chart import BoardTest, CT, KANBAN, LANES, NOW, TODAY, TODAY_TRACKER, YESTERDAY, at, flow_launch_tracker, merged  # noqa: E402
from test_pages import get  # noqa: E402

try:
    import issue_page as ip  # noqa: E402
except ModuleNotFoundError:  # red until the implementer creates it; each test then fails on the import
    ip = None

HEAD = "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n" + "|---" * 11 + "|\n"
YESTERDAY_TRACKER = f"""# Tracker

## Tasks

{HEAD}| Upload size limit | Cap uploads | Robin | running 22:00 | {YESTERDAY} |  | M | build | implement | #109 | c |
| Cache warmup | Warm the pool | Robin | running 23:00 | {YESTERDAY} |  | S | build | implement | #111 | c |

## Log

- 22:00 stage: Upload size limit → implement
- 23:00 stage: Cache warmup → implement
"""
TODAY_START = f"""# Tracker

## Tasks

{HEAD}| Upload size limit | Cap uploads | Robin | waiting | {YESTERDAY} |  | M | build | pr | #109 | c |
| Review round 1 | Review r1 | unassigned | open | {TODAY} |  | S |  |  | #109 | c |
| Verify limits | Verify r1 | unassigned | open | {TODAY} |  | S |  |  | #109 | c |
| Fix round 1 | Fix r1 | unassigned | open | {TODAY} |  | S |  |  | #109 | c |
| Cache warmup | Warm the pool | unassigned | open | {YESTERDAY} |  | S | build | implement | #111 | c |
| Tidy config | Tidy | Robin | open | {TODAY} |  | S |  |  |  | c |

## File ownership

| context | paths |
|---|---|
| Review r1 | `src/a/` |
| Verify r1 | `src/d/` |
| Fix r1 | `src/b/` |
| Warm the pool | `src/c/` |

## Log

- 00:01 opened the day
"""
# The Log times of #109's stage and launcher lines, oldest first; then the times of the other tasks' lines.
TIMELINE = ("22:00", "00:05", "00:20", "00:30", "00:50", "01:00", "01:20")
OTHERS = ("23:00", "00:40", "01:50")


def today_tracker() -> str:
    """Today's tracker as the launcher's and the coordinator's own writers leave it."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tracker.md"
        path.write_text(tw.append_log(TODAY_START, "- 00:05 stage: Upload size limit → pr"))
        one_shot.record_launch(path, "Review r1", "impl-a1", "worktree `a` (a)", "codex", "gpt-5.5", "00:20")
        one_shot.record_launch(path, "Verify r1", "impl-d4", "worktree `d` (d)", "claude", "sonnet", "00:30")
        one_shot.record_launch(path, "Warm the pool", "impl-c3", "worktree `c` (c)", "codex", "gpt-5.5", "00:40")
        one_shot.update_tracker(path, "Verify r1", "impl-d4", "Robin", "human_review", "unclear", "v.py", "00:50")
        one_shot.update_tracker(path, "Review r1", "impl-a1", "Robin", "done", "reviewed", "r.py", "01:00")
        one_shot.record_launch(path, "Fix r1", "impl-b2", "worktree `b` (b)", "claude", "opus", "01:20")
        path.write_text(tw.append_log(path.read_text(), "- 01:50 stage: Cache warmup → pr"))
        return path.read_text()


ISSUE = bs.Issue("#109", "https://forge.example/grp/app/-/issues/109", "Upload size limit", "open", ())
MR = bs.Change("!48", "https://forge.example/grp/app/-/merge_requests/48", "Cap uploads at 25 MB", "open", False,
               "failed", False, 2, None, "main", (), ("#109",))
OTHER = bs.Change("!50", "https://forge.example/grp/app/-/merge_requests/50", "Warm the pool", "open", False,
                  "passed", True, 1, None, "main", (), ("#111",))


def sources(*changes: bs.Change, issue: bs.Issue = ISSUE) -> bs.Sources:
    return bs.Sources({}, {"#109": issue}, {c.ref: c for c in changes}, (), {})


def page(number: int = 109, src: bs.Sources | None = None, today: str | None = None,
         trackers: list | None = None) -> str | None:
    if ip is None:
        raise AssertionError("scripts/issue_page.py does not exist yet")
    trackers = trackers or [(YESTERDAY, YESTERDAY_TRACKER), (TODAY, today if today is not None else today_tracker())]
    return ip.render(number, trackers, src if src is not None else sources(MR, OTHER), NOW, LANES)


def text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def section(html: str, heading: str) -> str:
    """From the `<h2>` that starts with `heading` to the next `<h2>`."""
    m = re.search(rf"<h2[^>]*>\s*{heading}\b", html, re.I)
    if not m:
        raise AssertionError(f"no {heading!r} section")
    end = html.find("<h2", m.end())
    return html[m.start():end if end >= 0 else len(html)]


class RenderTest(unittest.TestCase):
    def test_no_header_prints_a_rendered_clock(self) -> None:
        # #215: the task header's eyebrow keeps the board link and the lane, not a render clock.
        self.assertNotRegex(page(), r"\brendered \d{1,2}:\d{2}")

    def test_an_issue_no_task_names_has_no_page(self):
        # Route: /issues/<n> exists only for an issue a tracker task names.
        self.assertIsNone(page(110))

    def test_the_header_names_the_issue_the_task_and_its_state(self):
        # Header: "Task · #109 · … · board", h1 "Upload size limit", chip "NEEDS INPUT": the row waits on Robin, and
        # the task page holds it as the board does (#228).
        header = re.search(r"<header\b.*?</header>", page(), re.S)
        self.assertIsNotNone(header)
        self.assertIn("#109", text(header[0]))
        self.assertIn('href="/"', header[0])
        self.assertIn("Upload size limit", text(re.search(r"<h1\b.*?</h1>", header[0], re.S)[0]))
        self.assertRegex(text(header[0]), r"(?i)\bneeds input\b")

    def test_flow_is_one_row_of_every_task_on_the_issue(self):
        # FLOW: one row, the issue's tasks merged by flow_chart.build; no other issue's row.
        html = page()
        today = today_tracker()
        built = fc.build(fc.moves(YESTERDAY_TRACKER, YESTERDAY, CT) + fc.moves(today, TODAY, CT),
                         rb.parse_tracker(YESTERDAY_TRACKER).tasks + rb.parse_tracker(today).tasks, LANES,
                         task_forge.held_names(task_forge.hold_causes(rb.parse_tracker(today).tasks, None, sources(MR, OTHER), LANES, set())), {}, NOW)  # #228
        row = next(r for _, r in built if r.ref == "#109")
        figures = re.findall(r'<figure class="gantt".*?</figure>', html, re.S)
        self.assertTrue(figures)
        for fig in figures:
            self.assertEqual(fig.count('<div class="gantt-row">'), 1)
        self.assertEqual(set(re.findall(r'class="gantt-ref"[^>]*>([^<]*)<', html)), {"#109"})
        self.assertEqual(set(re.findall(r'class="gantt-name">([^<]*)<', html)), {row.name})
        for g in row.segments:
            with self.subTest(segment=(g.category, f"{g.start:%H:%M}")):
                self.assertRegex(html, rf'title="{g.category} (?:\w{{3}} )?{g.start:%H:%M}–')

    def test_the_change_card_shows_the_change_that_closes_the_issue(self):
        # MERGE REQUEST card: ref and link, state, pipeline, approvals; another issue's change is not shown.
        html = page()
        refs = section(html, "References")
        outside = html.replace(refs, "")
        self.assertIn("!48", text(outside))
        self.assertIn(f'href="{MR.url}"', outside)
        self.assertRegex(text(outside), r"(?i)pipeline\W+failed")
        self.assertRegex(text(outside), r"(?i)approv\w*\W+2\b|\b2\s+approv")
        self.assertNotIn("closed", text(outside).lower())
        closed = page(src=sources(replace(MR, state="closed"), OTHER))
        self.assertIn("closed", text(closed).lower())
        for other in ("!50", OTHER.url):
            self.assertNotIn(other, html)

    def test_no_change_is_one_line_instead_of_the_card(self):
        # MERGE REQUEST card: with no change naming the issue, the page says so.
        html = page(src=sources(OTHER))
        self.assertNotIn("!50", html)
        self.assertNotRegex(text(html), r"(?i)pipeline\W+(?:passed|failed)")
        self.assertRegex(text(html), r"(?i)\bno\b\W+(?:\w+\W+){0,2}(?:change|merge request|pull request|MR|PR)\b")

    def test_how_it_got_here_is_the_issues_log_lines_in_time_order(self):
        # HOW IT GOT HERE: stage moves and the launcher's started, completed and review lines, oldest first.
        got = text(section(page(), "How it got here"))
        at = [got.find(t) for t in TIMELINE]
        self.assertNotIn(-1, at, got)
        self.assertEqual(at, sorted(at))
        for name in ("impl-a1", "impl-d4", "impl-b2"):
            self.assertIn(name, got)
        for other in (*OTHERS, "impl-c3", "Cache warmup"):
            with self.subTest(other=other):
                self.assertNotIn(other, got)

    def test_workers_are_the_ones_launched_on_the_issue(self):
        # WORKERS: name, runtime and model, and RUNNING while its task row reads running.
        got = text(section(page(), "Workers"))
        self.assertNotIn("impl-c3", got)
        workers = {"impl-a1": ("codex", "gpt-5.5", False), "impl-d4": ("claude", "sonnet", False),
                   "impl-b2": ("claude", "opus", True)}
        starts = sorted((got.find(name), name) for name in workers)
        self.assertNotIn(-1, [s for s, _ in starts], got)
        for (start, name), (end, _) in zip(starts, starts[1:] + [(len(got), "")]):
            runtime, model, running = workers[name]
            one = got[start:end]
            with self.subTest(worker=name):
                self.assertIn(runtime, one)
                self.assertIn(model, one)
                self.assertEqual(bool(re.search(r"(?i)\brunning\b", one)), running)

    def test_references_link_the_issue_and_its_change(self):
        # REFERENCES: the issue, and the change when one is known.
        refs = section(page(), "References")
        self.assertIn(f'href="{ISSUE.url}"', refs)
        self.assertIn(f'href="{MR.url}"', refs)
        refs = section(page(src=sources()), "References")
        self.assertIn(f'href="{ISSUE.url}"', refs)
        self.assertNotIn("merge_requests", refs)


# Stage 2. The changes into `main` around !48: !9 and !41 merge before it and !50 after; !45 targets another base and
# !30 is merged, so neither is in main's queue. Only !50 shares a file (src/upload/limits.py) with !48.
FILES = ("src/upload/limits.py", "src/upload/form.py")
MINE = replace(MR, files=FILES)


def change(n: int, state: str, base: str, files: tuple[str, ...], issue: str) -> bs.Change:
    return bs.Change(f"!{n}", f"https://forge.example/grp/app/-/merge_requests/{n}", f"Change {n}", state, False,
                     "passed", False, 0, NOW if state == "merged" else None, base, files, (issue,))


QUEUE = (change(9, "open", "main", ("src/log.py",), "#130"), change(41, "open", "main", ("src/common/config.py",), "#120"),
         change(45, "open", "release", ("src/release/notes.py",), "#121"), change(30, "merged", "main", ("src/upload/form.py",), "#131"),
         replace(OTHER, files=("src/upload/limits.py", "src/cache/pool.py")))


def card(html: str) -> str:
    """The change card: the section headed "Merge request" (or "Pull request")."""
    return section(html, r"(?:Merge|Pull) request")


def paired(got: str, pairs: dict[str, str]) -> list[str]:
    """Asserts each key (a literal) sits next to a match of its value (a pattern), all on the same side, among the
    keys and values in reading order; so a row may read `name status` or `status name`. Returns that sequence."""
    seq = re.findall("|".join([*map(re.escape, pairs), *(f"(?:{v})" for v in pairs.values())]), got)

    def beside(d: int) -> bool:
        return all(k in seq and 0 <= (i := seq.index(k)) + d < len(seq) and re.fullmatch(v, seq[i + d])
                   for k, v in pairs.items())
    if not (beside(1) or beside(-1)):
        raise AssertionError(f"not each beside its own {pairs}: {seq}")
    return seq


def with_stats(c: bs.Change, *rows: tuple[str, int, int]) -> bs.Change:
    return replace(c, files=tuple(p for p, _, _ in rows),
                   stats=tuple(bs.FileStat(path=p, additions=a, deletions=d) for p, a, d in rows))


def thread(resolved: bool, hhmm: str, body: str) -> "bs.ReviewThread":
    at = datetime.combine(TODAY, time.fromisoformat(hhmm), CT)
    return bs.ReviewThread(resolved=resolved, author="reviewer-a", body=body, at=at,
                           url=f"https://forge.example/grp/app/-/merge_requests/48#note_{hhmm.replace(':', '')}")


class MergeOrderTest(unittest.TestCase):
    """A1: "merge order K of M": M is the open changes into the same base, ordered by change number, oldest first,
    and the card lists the refs merging before it. A merged or closed change shows no merge order."""

    def test_merge_order_counts_open_changes_into_the_same_base_by_number(self):
        got = text(card(page(src=sources(MINE, *QUEUE))))
        self.assertRegex(got, r"(?i)merge order\W+3 of 4\b")
        self.assertRegex(got, r"(?i)\bafter\W+!9\W+!41\b")
        for other in ("!30", "!45"):
            with self.subTest(other=other):
                self.assertNotIn(other, got)

    def test_a_merged_or_closed_change_shows_no_merge_order(self):
        self.assertRegex(text(card(page(src=sources(MINE, *QUEUE)))), r"(?i)merge order")  # while open, it does
        for state in ("merged", "closed"):
            with self.subTest(state=state):
                got = text(card(page(src=sources(replace(MINE, state=state), *QUEUE))))
                self.assertNotRegex(got, r"(?i)merge order")


class OverlapTest(unittest.TestCase):
    """A2: "overlap N file(s)" counts files shared with other open changes, naming the other change and a shared
    path; with none it says "overlap none"."""

    def test_overlap_names_the_other_open_change_and_a_shared_path(self):
        got = text(card(page(src=sources(MINE, *QUEUE))))
        self.assertRegex(got, r"(?i)overlap\W+1 file\b")  # form.py is shared only with !30, which is merged
        self.assertIn("!50", got)
        self.assertIn("src/upload/limits.py", got)

    def test_no_shared_file_is_overlap_none(self):
        got = text(card(page(src=sources(MINE, change(41, "open", "main", ("src/common/config.py",), "#120")))))
        self.assertRegex(got, r"(?i)overlap\W+none\b")


class FilesChangedTest(unittest.TestCase):
    """A3: Files changed lists each file the change touches; one another open change also touches is marked "also !N".
    B1: each file shows "+A −D", and a total line."""

    def test_each_file_is_listed_and_a_shared_one_names_the_other_change(self):
        got = text(section(page(src=sources(MINE, *QUEUE)), "Files changed"))
        for path in FILES:
            self.assertIn(path, got)
        paired(got, {"src/upload/limits.py": r"also\W+!50"})
        self.assertEqual(len(re.findall(r"(?i)\balso\b", got)), 1, got)  # only merged !30 touches form.py

    def test_each_file_shows_its_additions_and_deletions_and_a_total(self):
        mine = with_stats(MR, ("src/upload/limits.py", 12, 3), ("src/upload/form.py", 5, 2))
        got = text(section(page(src=sources(mine, OTHER)), "Files changed"))
        paired(got, {"src/upload/limits.py": r"\+12\s*[−-]3", "src/upload/form.py": r"\+5\s*[−-]2"})
        self.assertRegex(got, r"\+17\s*[−-]5\b")


class ArchitectureTest(unittest.TestCase):
    """A4: ARCHITECTURE reuses the decision page's module-graph behaviour for the issue's change."""

    def test_with_no_graph_table_it_says_the_graph_is_not_configured(self):
        got = section(page(), "Architecture")
        self.assertIn("not configured", got)
        self.assertNotIn('class="mermaid"', got)

    def test_with_no_change_there_is_no_graph(self):
        got = section(page(src=sources(OTHER)), "Architecture")
        self.assertNotIn('class="mermaid"', got)
        self.assertNotIn("not configured", got)


class PipelineTest(unittest.TestCase):
    """B2: the card lists each of the head pipeline's jobs with its status."""

    def test_the_card_lists_each_job_with_its_status(self):
        jobs = {"lint-code": "passed", "unit-tests": "failed", "preview-deploy": "running"}
        mine = replace(MR, jobs=(bs.Job(name="lint-code", status="passed", seconds=42),
                                 bs.Job(name="unit-tests", status="failed", seconds=310),
                                 bs.Job(name="preview-deploy", status="running", seconds=None)))
        paired(text(card(page(src=sources(mine, OTHER)))).lower(), jobs)


class ConflictsTest(unittest.TestCase):
    """B3: the card shows "conflicts none" or "conflicts yes"."""

    def test_the_card_says_whether_the_change_conflicts_with_its_base(self):
        for conflicts, word in ((False, "none"), (True, "yes")):
            with self.subTest(conflicts=conflicts):
                got = text(card(page(src=sources(replace(MR, conflicts=conflicts), OTHER))))
                self.assertRegex(got, rf"(?i)conflicts\W+{word}\b")


class ReviewTest(unittest.TestCase):
    """B4: REVIEW lists the threads newest first, open before resolved, each marked Open or Resolved; the card shows
    "threads K open" and the resolved count; with none the section says the change has no review threads."""

    def threads(self) -> tuple:
        return (thread(False, "00:30", "Cap should be configurable"), thread(True, "01:00", "Typo in the message"),
                thread(False, "01:30", "Test exactly 25 MB"), thread(True, "00:10", "Rename the helper"))

    def test_open_threads_come_first_then_resolved_each_newest_first(self):
        order = ("Test exactly 25 MB", "Cap should be configurable", "Typo in the message", "Rename the helper")
        got = text(re.sub(r"<h2\b.*?</h2>", "", section(page(src=sources(replace(MR, threads=self.threads()), OTHER)),
                                                          "Review"), flags=re.S))
        at = [got.find(body) for body in order]
        self.assertNotIn(-1, at, got)
        self.assertEqual(at, sorted(at))
        self.assertEqual([m.lower() for m in re.findall(r"(?i)\b(?:open|resolved)\b", got)],
                         ["open", "open", "resolved", "resolved"], got)

    def test_the_card_counts_open_and_resolved_threads(self):
        got = text(card(page(src=sources(replace(MR, threads=self.threads()[:3]), OTHER))))
        self.assertRegex(got, r"(?i)threads\W+2 open\b")
        self.assertRegex(got, r"(?i)\b1 resolved\b")

    def test_no_threads_says_so(self):
        got = text(section(page(src=sources(replace(MR, threads=()), OTHER)), "Review"))
        self.assertRegex(got, r"(?i)\bno review threads\b")


BODY = """## Context

Uploads have no cap.

{heading} Acceptance

- [x] Uploads over 25 MB are refused
- [ ] The limit is configurable
- The error names the limit

## Notes

- not a criterion
"""


class AcceptanceTest(unittest.TestCase):
    """C2: ACCEPTANCE lists the bullets under the body's "Acceptance" heading (any level); `- [x]` is met, `- [ ]`
    and plain `-` are not; the heading shows "K of N met". C3: with no such heading, it says the issue states none."""

    def test_the_bullets_under_acceptance_are_listed_and_counted(self):
        for heading in ("#", "###"):
            with self.subTest(heading=heading):
                issue = replace(ISSUE, body=BODY.format(heading=heading))
                got = section(page(src=sources(MR, OTHER, issue=issue)), "Acceptance")
                self.assertRegex(text(re.search(r"<h2\b.*?</h2>", got, re.S)[0]), r"\b1 of 3 met\b")
                for bullet in ("Uploads over 25 MB are refused", "The limit is configurable", "The error names the limit"):
                    self.assertIn(bullet, text(got))
                self.assertNotIn("not a criterion", text(got))

    def test_no_acceptance_heading_says_the_issue_states_none(self):
        issue = replace(ISSUE, body="## Context\n\n- [ ] something\n")
        got = text(section(page(src=sources(MR, OTHER, issue=issue)), "Acceptance"))
        self.assertRegex(got, r"(?i)\bno acceptance criteria\b")


DONE_TRACKER = f"""# Tracker

## Tasks

{HEAD}| Upload size limit | Cap uploads | Robin | done 01:00 | {TODAY} |  | M | build | main | #109 | c |

## Log

- 00:30 stage: Upload size limit → merge
- 01:00 stage: Upload size limit → main
"""
DUE = datetime.combine(TODAY, time(5, 0), CT)


def due_tracker() -> str:
    """Today's tracker with #109's build row due 05:00 and running, so the Flow row forecasts its remaining stages: a
    row waiting on Robin is held, and a held row forecasts nothing (#228)."""
    before = today_tracker()
    after = before.replace(f"| Robin | waiting | {YESTERDAY} |  |", f"| Robin | running 00:05 | {YESTERDAY} | {DUE:%H:%M} |")
    assert after != before, "the fixture's #109 row moved"
    return after


class WhatHappensNextTest(unittest.TestCase):
    """D1: the forecast stages of the task's Flow row, "<stage> ~HH:MM–HH:MM". D2: an open change's merge-queue
    position. D3: done or closed says nothing next; open with no forecast says no estimate."""

    def test_forecast_stages_from_the_flow_row(self):
        today = due_tracker()
        built = fc.build(fc.moves(YESTERDAY_TRACKER, YESTERDAY, CT) + fc.moves(today, TODAY, CT),
                         rb.parse_tracker(YESTERDAY_TRACKER).tasks + rb.parse_tracker(today).tasks, LANES, {},
                         {"Cap uploads": DUE}, NOW)
        ahead = [g for _, r in built if r.ref == "#109" for g in r.segments if g.kind == "forecast"]
        self.assertTrue(ahead, "the fixture forecasts nothing")
        got = text(section(page(today=today), "What happens next"))
        at = []
        for g in ahead:
            with self.subTest(stage=g.category):
                m = re.search(rf"{g.category}\s*~{g.start.astimezone(CT):%H:%M}\s*[–-]\s*{g.end.astimezone(CT):%H:%M}", got)
                self.assertIsNotNone(m, got)
                at.append(m.start())
        self.assertEqual(at, sorted(at))

    def test_an_open_change_gives_its_merge_queue_position(self):
        got = text(section(page(src=sources(MINE, *QUEUE)), "What happens next"))
        self.assertRegex(got, r"\b3 of 4\b")
        self.assertRegex(got, r"(?i)\bafter\W+!9\W+!41\b")

    def test_a_change_in_another_repo_is_not_in_this_repos_merge_line(self):
        # Another repo's change is labelled `owner/repo#N`; reading its number as `ref[1:]` broke the page.
        elsewhere = replace(change(7, "open", "main", ("src/other.py",), "#140"), ref="grp/other#7")
        got = text(section(page(src=sources(MINE, *QUEUE, elsewhere)), "What happens next"))
        self.assertRegex(got, r"\b3 of 4\b")

    def test_an_open_task_with_no_forecast_has_no_estimate(self):
        got = text(section(page(), "What happens next"))
        self.assertRegex(got, r"(?i)\bno estimate\b")

    def test_a_done_or_closed_task_has_nothing_next(self):
        done = page(src=sources(replace(MR, state="merged")), trackers=[(TODAY, DONE_TRACKER)])
        closed = page(src=sources(MR, OTHER, issue=replace(ISSUE, state="closed")))
        for name, html in (("done", done), ("closed", closed)):
            with self.subTest(task=name):
                got = text(section(html, "What happens next"))
                self.assertRegex(got, r"(?i)\bnothing\b.*\bnext\b")
                self.assertNotRegex(got, r"(?i)\bno estimate\b")


class FlowRefReviewTest(unittest.TestCase):
    """PR 461 R2: an issue page excludes change-keyed Flow rows with the same number."""

    def test_an_issue_page_does_not_list_a_change_flow_row_with_the_same_number(self):
        for url in ("https://forge.example/group/project/-/merge_requests/51",
                    "https://github.com/group/project/pull/51"):
            with self.subTest(url=url):
                review = f"Review {url} and check diagnostics"
                tracker = flow_launch_tracker([
                    ("Build parser", "Build the parser", "#51"),
                    ("Review change", review, "workflow")],
                    [("Build the parser", "worker-1", "00:10"), (review, "worker-2", "00:40")])
                html = page(51, src=bs.Sources({}, {}, {}, (), {}), trackers=[(TODAY, tracker)])
                self.assertIsNotNone(html)
                flow = section(html, "Flow")
                self.assertEqual(set(re.findall(r'class="gantt-name">([^<]*)<', flow)), {"Build parser"})
                self.assertNotIn("Review change", flow)

    def test_issue_key_and_issue_url_flow_rows_still_appear_on_the_issue_page(self):
        for ref in ("group/project#7", "https://forge.example/group/project/-/issues/7",
                    "https://github.com/group/project/issues/7"):
            with self.subTest(ref=ref):
                tracker = flow_launch_tracker([("Build parser", "Build the parser", ref)],
                                              [("Build the parser", "worker-1", "00:10")])
                html = page(7, src=bs.Sources({}, {}, {}, (), {}), trackers=[(TODAY, tracker)])
                self.assertIsNotNone(html)
                self.assertEqual(set(re.findall(r'class="gantt-name">([^<]*)<', section(html, "Flow"))),
                                 {"Build parser"})


class BoardLinkTest(unittest.TestCase):
    def test_a_flow_row_for_an_issue_links_to_its_page(self):
        # Flow row → Task page: the board's row for #109 opens /issues/109.
        cfg = parse_coordinator("## Coordinator\n\n- User: Robin\n- Daily log dir: `daily/`\n"
                                   "- Tracker: `daily/<date>-tracker.md`\n- Timezone: America/Chicago\n", today=TODAY)
        html = rb.render(TODAY_TRACKER, cfg, NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN)
        flow = html[html.index("<h2>Flow ·"):]
        refs = set(re.findall(r'class="gantt-ref"[^>]*>([^<]+)<', flow))
        self.assertEqual(refs, {"#109", "#111", "#98"})
        self.assertEqual(set(re.findall(r'href="(/issues/[^"]*)"', flow)), {f"/issues/{r[1:]}" for r in refs})


# Compact workers and history (Task.dc.html: WORKERS, HOW IT GOT HERE, the line under FLOW). A task's item is the
# launcher's task text, often a long prompt; the launcher writes it whole into its started line.
LONG = ("Cap uploads at 25 MB across the form and the API. The form refuses a larger file before sending it and "
        "names the limit in its message. Keep the limit in one setting that both sides read, so a later change edits "
        "one place. Add tests for exactly 25 MB, one byte over, and an empty file. Leave the storage layer and the "
        "retry logic alone, since another task owns those files. When done, open a merge request that closes the "
        "issue and lists what was tested.")
LONG_REVIEW = ("Review the upload limit change against the acceptance list and the error format guide. Leave one "
               "thread per finding with the file and line, and say whether each is blocking. Check that the tests "
               "cover the boundary sizes the issue names, and that nothing outside the upload module changed.")
SENTENCES = ("Keep the limit in one setting that both sides read", "Leave one thread per finding with the file and line")
TREE_W, TREE_R = "worktree `trees/upload-limit` (feat/upload-limit)", "worktree `trees/upload-review` (feat/upload-review)"
WORK_YESTERDAY = f"""# Tracker

## Tasks

{HEAD}| Upload size limit | {LONG} | unassigned | open | {YESTERDAY} |  | M | build | implement | #109 | c |

## File ownership

| context | paths |
|---|---|
| Upload size limit | `src/upload/` |

## Log

- 20:00 stage: Upload size limit → implement
"""
WORK_TODAY = f"""# Tracker

## Tasks

{HEAD}| Upload size limit | {LONG} | unassigned | open | {YESTERDAY} |  | M | build | review | #109 | c |
| Review round 1 | {LONG_REVIEW} | unassigned | open | {TODAY} |  | S | build | review | #109 | c |

## File ownership

| context | paths |
|---|---|
| Upload size limit | `src/upload/` |
| Review round 1 | none; read-only review of `src/upload/` |

## Log

- 00:00 stage: Upload size limit → review
- 00:10 stage: Review round 1 → review
"""
REOPEN = ("| Robin | waiting |", "| unassigned | open |")


def work_trackers() -> list:
    """impl-w1 is launched three times on "Upload size limit": 20:05–21:35 and 22:00–23:35 yesterday at implement,
    00:20–01:10 today at review. rev-w2 is launched once on "Review round 1" at review, 00:40, still running at NOW
    (02:10), so it overlaps impl-w1's 00:20–01:10 in time. The review owns nothing (read-only), so the launcher, which refuses
    a launch whose File ownership overlaps a running row's, lets it start while impl-w1 runs.
    Workers: impl-w1 1h30m + 1h35m + 0h50m = 3h55m; rev-w2 1h30m; header 5h25m · 2 sessions.
    Flow: worked (the union) 1h30m + 1h35m + 00:20→02:10 1h50m = 4h55m; elapsed 20:05 yesterday → 02:10 = 6h05m;
    waiting 1h10m. Summing without the union would give 5h25m."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tracker.md"
        path.write_text(WORK_YESTERDAY)
        one_shot.record_launch(path, LONG, "impl-w1", TREE_W, "claude", "opus", "20:05")
        one_shot.update_tracker(path, LONG, "impl-w1", "Robin", "done", "implemented", "src/upload/limits.py", "21:35")
        path.write_text(path.read_text().replace(*REOPEN))
        one_shot.record_launch(path, LONG, "impl-w1", TREE_W, "claude", "opus", "22:00")
        one_shot.update_tracker(path, LONG, "impl-w1", "Robin", "done", "fixed the limit", "src/upload/form.py", "23:35")
        yesterday = path.read_text()
        path.write_text(WORK_TODAY)
        one_shot.record_launch(path, LONG, "impl-w1", TREE_W, "claude", "opus", "00:20")
        one_shot.record_launch(path, LONG_REVIEW, "rev-w2", TREE_R, "codex", "gpt-5.5", "00:40")
        one_shot.update_tracker(path, LONG, "impl-w1", "Robin", "done", "answered review", "src/upload/form.py", "01:10")
        return [(YESTERDAY, yesterday), (TODAY, path.read_text())]


def work_page() -> str:
    return page(src=sources(MR), trackers=work_trackers())


def rows(got: str, names: tuple[str, ...]) -> dict[str, str]:
    """Each name's slice of `got`, from the name to the next name (or the end)."""
    starts = sorted((got.find(n), n) for n in names)
    if -1 in [s for s, _ in starts]:
        raise AssertionError(f"not every worker in {got!r}")
    return {n: got[s:e] for (s, n), (e, _) in zip(starts, starts[1:] + [(len(got), "")])}


class CompactWorkersTest(unittest.TestCase):
    """Workers and How it got here are compact lines, not the launcher's whole Log line; Flow shows worked vs
    waiting."""

    def test_no_line_carries_the_task_prompt(self):
        # Rule 1: "No Workers row and no How-it-got-here line carries the task prompt text." The task is named by
        # its tracker name at most.
        html = work_page()
        self.assertIn("rev-w2", text(section(html, "Workers")))  # the fixture launched its workers
        for sentence in SENTENCES:
            with self.subTest(sentence=sentence):
                self.assertNotIn(sentence, text(html))
        for heading in ("Workers", "How it got here"):
            got = text(section(html, heading))
            for item in (LONG, LONG_REVIEW):
                with self.subTest(section=heading, item=item[:20]):
                    self.assertNotIn(item[:40], got)

    def test_one_row_per_worker(self):
        # Rule 2: "Each row shows the worker's name, its kind with ×N when it was launched N>1 times, its runtime and
        # model, the stages it worked, its total run time and its state." Stages: "the task's stage at each launch,
        # in order, without repeats." Run time: "the sum of each launch's start to end, or start to now while it is
        # still running", as <h>h<mm>m.
        got = rows(text(re.sub(r"<h2\b.*?</h2>", "", section(work_page(), "Workers"), flags=re.S)), ("impl-w1", "rev-w2"))
        w1, w2 = got["impl-w1"], got["rev-w2"]
        self.assertRegex(w1, r"one-shot\s*×3\b")
        self.assertNotRegex(w2, r"×\s*\d")
        self.assertRegex(w2, r"\bone-shot\b")
        self.assertRegex(w1, r"\bclaude\W+opus\b")
        self.assertRegex(w2, r"\bcodex\W+gpt-5\.5\b")
        self.assertRegex(w1, r"\bimplement\W+review\b")
        self.assertEqual(len(re.findall(r"\bimplement\b", w1)), 1, w1)
        self.assertEqual(len(re.findall(r"\breview\b", w1)), 1, w1)
        self.assertEqual(len(re.findall(r"\breview\b", w2)), 1, w2)
        self.assertRegex(w1, r"(?<![\dh])3h55m\b")
        self.assertRegex(w2, r"(?<![\dh])1h30m\b")
        self.assertNotRegex(w1, r"(?i)\brunning\b")
        self.assertRegex(w1, r"(?i)\bcompleted\b|\bdone\b")
        self.assertRegex(w2, r"(?i)\brunning\b")

    def test_the_workers_header_sums_run_time_and_counts_workers(self):
        # Rule 3: "It reads `Workers · <sum of all workers' run time> · <number of distinct workers> sessions`."
        h2 = text(re.search(r"<h2\b.*?</h2>", section(work_page(), "Workers"), re.S)[0])
        self.assertRegex(h2, r"(?i)^Workers\s*·\s*5h25m\s*·\s*2 sessions$")

    def test_how_it_got_here_is_one_short_line_per_launcher_event(self):
        # Rule 4: "Each line has the time, then `<stage> started`, or `ended <status>`, then the worker, then the
        # tree. Lines are in time order. Stage moves that already appear stay as they are."
        got = text(section(work_page(), "How it got here"))
        w, r = r"impl-w1\W+trees/upload-limit\b", r"rev-w2\W+trees/upload-review\b"
        lines = (r"20:00\W+stage: Upload size limit → implement",
                 rf"20:05\W+implement started\W+{w}", rf"21:35\W+ended completed\W+{w}",
                 rf"22:00\W+implement started\W+{w}", rf"23:35\W+ended completed\W+{w}",
                 r"00:00\W+stage: Upload size limit → review", r"00:10\W+stage: Review round 1 → review",
                 rf"00:20\W+review started\W+{w}", rf"00:40\W+review started\W+{r}", rf"01:10\W+ended completed\W+{w}")
        at = []
        for line in lines:
            with self.subTest(line=line):
                m = re.search(rf"(?i){line}", got)
                self.assertIsNotNone(m, got)
                at.append(m.start())
        self.assertEqual(at, sorted(at))

    def test_flow_shows_time_worked_of_time_elapsed_and_waiting(self):
        # Rule 5: "It reads `Worked <A> of <B> elapsed · <C> waiting`": A is the union of all launch intervals, so
        # overlaps count once; B runs from the first launch to the task's end, or to now while open; C = B − A.
        html = work_page()
        start = re.search(r"<h2[^>]*>\s*Flow\b", html)
        self.assertIsNotNone(start, "no Flow section")
        after = next((m.start() for m in re.finditer(r"<h2[^>]*>\s*(?!Flow\b)", html[start.end():])), None)
        flow = text(html[start.start():start.end() + after if after is not None else len(html)])
        self.assertRegex(flow, r"(?i)\bWorked\s+4h55m\s+of\s+6h05m\s+elapsed\s*·\s*1h10m\s+waiting\b")


ZONE = "America/Chicago"
ROUTE_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Upload size limit | Cap uploads | Robin | open | {day} |  | S |  |  | #109 | c |

## Log

- 00:01 opened the day
- 00:02 noted #110 for later
- 00:03 stage: Upload size limit → implement
"""


class RouteTest(unittest.TestCase):
    """pages.py serves `/issues/<n>`: a route, rendered on request, whose file is never served by name."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n")
        (self.root / "daily").mkdir()
        (self.root / "daily" / f"{day}-tracker.md").write_text(ROUTE_TRACKER.format(day=day))
        self.pages = self.root / "pages"
        self.pages.mkdir()
        self.server = pg.make_server(self.pages, 0, root=self.root, refresh=lambda root, now: None, every=3600)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_an_issue_a_task_names_is_a_page(self):
        # Route: GET /issues/109 is the Task page for tracker issue #109.
        resp = get(self.port, "/issues/109")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Upload size limit", resp.body)
        self.assertIn(b"#109", resp.body)

    def test_an_issue_no_task_names_is_not_found(self):
        # Route: #110 is only mentioned in the Log, so it has no page.
        for path in ("/issues/110", "/issues/abc", "/issues/"):
            with self.subTest(path=path):
                self.assertEqual(get(self.port, path).status, 404)

    def test_the_rendered_file_is_not_served_by_name(self):
        # Route: pages are routes only (as /decisions/<slug>); an `.html` name is a 404.
        self.assertEqual(get(self.port, "/issues/109").status, 200)
        self.assertEqual(get(self.port, "/").status, 200)
        self.assertEqual(get(self.port, "/issues/109.html").status, 404)
        for name in sorted(p.name for p in self.pages.glob("*.html")):
            with self.subTest(name=name):
                self.assertEqual(get(self.port, f"/{name}").status, 404)


class ForgeMergedTaskPageTest(unittest.TestCase):
    """The task page reads merged when the forge does, in local time (#200)."""

    # America/Chicago is UTC-5 during 2026's daylight time, so this merge's local hour (18) differs from the UTC
    # hour `merged()` stamps it at (23): "...T23:40:49+00:00" is local 18:40.
    MERGE_LOCAL = "18:40"
    TODAY_RUNNING = TODAY_START.replace("| Robin | waiting |", "| Robin | running 01:00 |")
    NOW2 = datetime.combine(TODAY, time(20, 0), CT)

    def task_page(self, today: str, changes: tuple = (), now: datetime = NOW2) -> str:
        return ip.render(109, [(YESTERDAY, YESTERDAY_TRACKER), (TODAY, today)], sources(*changes), now, LANES)

    @staticmethod
    def flow_rows(render_call) -> dict[str, tuple]:
        """Every Flow row `render_call` draws, by name, as flow_chart.section receives them."""
        seen, real = [], fc.section
        with mock.patch.object(fc, "section", lambda rows, now: seen.append(rows) or real(rows, now)):
            render_call()
        return {row.name: (status, row) for status, row in seen[0]}

    def test_a_merged_change_shows_merged_not_the_trackers_word(self):
        # Acceptance 1: "shows a merged status, not the tracker's `waiting` or `running` word."
        for state, tracker in (("waiting", TODAY_START), ("running HH:MM", self.TODAY_RUNNING)):
            with self.subTest(state=state):
                html = self.task_page(tracker, (merged("#109", self.MERGE_LOCAL, 48),))
                header = re.search(r"<header\b.*?</header>", html, re.S)[0]
                chip = re.search(r'<span class="chip[^"]*">([^<]+)</span>', header)[1]
                self.assertRegex(chip, r"(?i)^merged\b", chip)
                self.assertNotRegex(chip, r"(?i)waiting|running", chip)

    def test_flow_rows_end_at_the_forge_merge_time_with_a_merged_note(self):
        # Acceptance 2: "Its Flow rows end at the forge merge time with the note `merged HH:MM`", as the board draws
        # the same task. No bar may end after the merge time.
        merge_at = at(TODAY, self.MERGE_LOCAL)
        rows = self.flow_rows(lambda: self.task_page(TODAY_START, (merged("#109", self.MERGE_LOCAL, 48),)))
        status, row = rows["Upload size limit"]
        self.assertEqual((status, row.note), ("merged", f"merged {self.MERGE_LOCAL}"))
        self.assertTrue(row.segments)
        for g in row.segments:
            self.assertLessEqual(g.end, merge_at, g)

    def test_elapsed_and_waiting_totals_stop_at_the_merge_time(self):
        # Acceptance 3: "Its elapsed and waiting totals stop at the merge time" — not keep counting past it. In
        # work_trackers() (#200's fixture from CompactWorkersTest), rev-w2 is still open at NOW (02:10); merged at
        # 01:40 it caps to "Worked 4h25m of 5h35m elapsed · 1h10m waiting", not the uncapped "4h55m of 6h05m".
        html = ip.render(109, work_trackers(), sources(merged("#109", "01:40", 48)), NOW, LANES)
        start = re.search(r"<h2[^>]*>\s*Flow\b", html)
        self.assertIsNotNone(start, "no Flow section")
        after = next((m.start() for m in re.finditer(r"<h2[^>]*>\s*(?!Flow\b)", html[start.end():])), None)
        flow = text(html[start.start():start.end() + after if after is not None else len(html)])
        self.assertRegex(flow, r"(?i)\bWorked\s+4h25m\s+of\s+5h35m\s+elapsed\s*·\s*1h10m\s+waiting\b", flow)

    def test_every_clock_is_in_the_workspace_zone(self):
        # Acceptance 4: "Every clock on the page, the merge time included, is in the workspace's timezone" — the
        # merge-request block's own chip too, not the UTC clock the forge cache stores merged_at in.
        html = self.task_page(TODAY_START, (merged("#109", self.MERGE_LOCAL, 48),))
        got = text(card(html))
        self.assertIn(self.MERGE_LOCAL, got)
        self.assertNotIn("23:40", got)

    def test_the_board_and_the_task_page_share_one_merged_map(self):
        # Acceptance 5: "The task page and the board get the merge time from one shared function." Through
        # behaviour, not by naming a function: render both from the same fixture and check the board's row note
        # equals the task page's note.
        cfg = BoardTest().cfg()
        src = sources(merged("#109", self.MERGE_LOCAL, 48))
        board_rows = self.flow_rows(lambda: rb.render(TODAY_START, cfg, self.NOW2, lanes=LANES, tracker_day=TODAY,
                                                       kanban=KANBAN, stage_log=[(YESTERDAY, YESTERDAY_TRACKER)],
                                                       sources=src))
        task_rows = self.flow_rows(lambda: ip.render(109, [(YESTERDAY, YESTERDAY_TRACKER), (TODAY, TODAY_START)],
                                                     src, self.NOW2, LANES))
        self.assertIn("Upload size limit", board_rows)
        self.assertIn("Upload size limit", task_rows)
        self.assertEqual(board_rows["Upload size limit"][1].note, task_rows["Upload size limit"][1].note)
        self.assertEqual(board_rows["Upload size limit"][1].note, f"merged {self.MERGE_LOCAL}")

    def test_the_board_and_the_task_page_name_the_same_hold_cause(self):
        # #228: a needs-input row names what holds it, a pending decision first, then a [kanban] hold stage, then the
        # person it waits on; the board's Flow and the task page read the one cause.
        cfg = BoardTest().cfg()
        src = sources()
        for kanban, refs, cause in ((KANBAN, {}, "Robin"), (replace(KANBAN, hold_stages=("pr",)), {}, "pr"),
                                    (replace(KANBAN, hold_stages=("pr",)), {"#109": None}, "decision")):
            with self.subTest(cause=cause):
                board = self.flow_rows(lambda: rb.render(TODAY_START, cfg, self.NOW2, lanes=LANES, tracker_day=TODAY,
                                                         kanban=kanban, stage_log=[(YESTERDAY, YESTERDAY_TRACKER)],
                                                         sources=src, pending_refs=refs))
                task = self.flow_rows(lambda: ip.render(109, [(YESTERDAY, YESTERDAY_TRACKER), (TODAY, TODAY_START)],
                                                        src, self.NOW2, LANES, kanban=kanban, pending_refs=refs))
                self.assertEqual(board["Upload size limit"][1].note, f"needs input · {cause}")
                self.assertEqual(task["Upload size limit"][1].note, f"needs input · {cause}")


DONE_MIDNIGHT_TRACKER = f"""# Tracker

## Tasks

{HEAD}| Upload size limit | Cap uploads | unassigned | open | {YESTERDAY} |  | M | build | implement | #109 | c |

## File ownership

| context | paths |
|---|---|
| Upload size limit | `src/upload/` |

## Log

- 20:00 stage: Upload size limit → implement
"""


def midnight_page(clock: str, now: datetime) -> str:
    """Cap uploads: launched yesterday 20:05, the one-shot's own `done` note lands at 22:15, and the
    coordinator closes the row `done <clock>` by hand that same evening (#213's repro shape)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tracker.md"
        path.write_text(DONE_MIDNIGHT_TRACKER)
        one_shot.record_launch(path, "Cap uploads", "impl-1", "worktree `a` (a)", "claude", "opus", "20:05")
        one_shot.update_tracker(path, "Cap uploads", "impl-1", "Robin", "done", "implemented the limit",
                                 "src/upload/limits.py", "22:15")
        closed = path.read_text().replace("| Robin | waiting |", f"| Robin | done {clock} |")
    return ip.render(109, [(YESTERDAY, closed)], sources(), now, LANES)


def flow_section(html: str) -> str:
    start = re.search(r"<h2[^>]*>\s*Flow\b", html)
    assert start, "no Flow section"
    after = next((m.start() for m in re.finditer(r"<h2[^>]*>\s*(?!Flow\b)", html[start.end():])), None)
    return text(html[start.start():start.end() + after if after is not None else len(html)])


# 17:16 yesterday plus 8h25m is 01:41 today; an hour later is 02:41. One minute of work is 17:16–17:17.
OPEN_NOW = at(TODAY, "01:41")
OPEN_LATER = at(TODAY, "02:41")
ATTEMPT_TREE = "worktree `trees/upload` (feat/upload)"
SIDE_TREE = "worktree `trees/side` (feat/side)"


def _tracker(rows: str, ownership: str, log: str = "") -> str:
    return f"""# Tracker

## Tasks

{HEAD}{rows}
## File ownership

| context | paths |
|---|---|
{ownership}
## Log

{log}"""


def _launch(path: Path, item: str, worker: str, tree: str, start: str, end: str | None, status: str) -> None:
    one_shot.record_launch(path, item, worker, tree, "claude", "opus", start)
    if end is not None:
        one_shot.update_tracker(path, item, worker, "Robin", status, "attempt ended", "src/upload/limits.py", end)


def ended_attempts(runs: tuple[tuple[str, str, str], ...], log: str = "",
                   final: tuple[str, str] | None = None) -> str:
    """One open issue, each run `(start, end, status)` on Cap uploads. `final` rewrites the last owner cell."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tracker.md"
        path.write_text(_tracker(
            f"| Upload size limit | Cap uploads | unassigned | open | {YESTERDAY} |  | M | build | implement | #109 | c |\n",
            "| Upload size limit | `src/upload/` |\n", log))
        for start, end, status in runs:
            if "| Robin | waiting |" in path.read_text():
                path.write_text(path.read_text().replace("| Robin | waiting |", "| unassigned | open |"))
            _launch(path, "Cap uploads", "impl-1", ATTEMPT_TREE, start, end, status)
        text_out = path.read_text()
    if final:
        text_out = text_out.replace(*final)
    return text_out


def overlapping_attempts() -> str:
    """Two ended workers on one issue, 17:16–18:16 and 17:46–18:16, so the second hour overlaps."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tracker.md"
        path.write_text(_tracker(
            f"| Upload size limit | Cap uploads | unassigned | open | {YESTERDAY} |  | M | build | implement | #109 | c |\n"
            f"| Side pass | Side item | unassigned | open | {YESTERDAY} |  | S | build | implement | #109 | c |\n",
            "| Upload size limit | `src/upload/` |\n| Side pass | `src/side/` |\n"))
        _launch(path, "Cap uploads", "impl-1", ATTEMPT_TREE, "17:16", "18:16", "human_review")
        _launch(path, "Side item", "impl-2", SIDE_TREE, "17:46", "18:16", "human_review")
        return path.read_text()


def worked_line(html: str) -> str:
    got = flow_section(html)
    found = re.search(r"Worked .+? waiting", got)
    if not found:
        raise AssertionError(f"no worked line in {got!r}")
    return found.group(0)


def open_page(tracker: str, now: datetime, *, pending: bool = False, issue: bs.Issue = ISSUE,
              day: date | None = None) -> str:
    refs = {"#109": None} if pending else frozenset()
    return ip.render(109, [(day or YESTERDAY, tracker)], sources(issue=issue), now, LANES, pending_refs=refs)


class OpenTaskWaitingTest(unittest.TestCase):
    """(#913) Elapsed for an open task runs to the render clock, so time after the last worker counts as waiting.
    A close still freezes the total. Worker outcome, a hold, and a forecast do not."""

    def test_a_failed_attempt_counts_the_wait_after_it_ends(self):
        tracker = ended_attempts((("17:16", "17:17", "human_review"),))
        self.assertEqual(worked_line(open_page(tracker, OPEN_NOW, pending=True)),
                         "Worked 1m of 8h25m elapsed · 8h24m waiting")

    def test_the_same_open_task_counts_another_hour_when_rendered_later(self):
        tracker = ended_attempts((("17:16", "17:17", "human_review"),))
        self.assertEqual(worked_line(open_page(tracker, OPEN_LATER, pending=True)),
                         "Worked 1m of 9h25m elapsed · 9h24m waiting")

    def test_a_completed_attempt_does_not_freeze_an_open_task(self):
        tracker = ended_attempts((("17:16", "17:17", "done"),))
        self.assertEqual(worked_line(open_page(tracker, OPEN_NOW, pending=True)),
                         "Worked 1m of 8h25m elapsed · 8h24m waiting")

    def test_a_hold_without_a_pending_decision_still_counts_idle_time(self):
        # Waiting on a person, and an open row with no owner, neither of which has an unanswered decision.
        held = ended_attempts((("17:16", "17:17", "human_review"),))
        free = ended_attempts((("17:16", "17:17", "human_review"),),
                              final=("| Robin | waiting |", "| unassigned | open |"))
        for name, tracker in (("held", held), ("free slot", free)):
            with self.subTest(name=name):
                self.assertEqual(worked_line(open_page(tracker, OPEN_NOW)),
                                 "Worked 1m of 8h25m elapsed · 8h24m waiting")

    def test_a_stage_move_at_the_worker_end_does_not_stop_an_open_task(self):
        tracker = ended_attempts((("17:16", "17:17", "human_review"),),
                                 log="- 17:00 stage: Upload size limit → implement\n- 17:17 stage: Upload size limit → review\n")
        self.assertEqual(worked_line(open_page(tracker, OPEN_NOW, pending=True)),
                         "Worked 1m of 8h25m elapsed · 8h24m waiting")

    def test_waiting_includes_the_gap_and_the_time_after_the_last_attempt(self):
        tracker = ended_attempts((("17:16", "17:17", "human_review"), ("18:00", "18:10", "human_review")))
        self.assertEqual(worked_line(open_page(tracker, OPEN_NOW, pending=True)),
                         "Worked 11m of 8h25m elapsed · 8h14m waiting")

    def test_overlapping_workers_count_once_and_idle_time_after_them_still_waits(self):
        self.assertEqual(worked_line(open_page(overlapping_attempts(), OPEN_NOW, pending=True)),
                         "Worked 1h00m of 8h25m elapsed · 7h25m waiting")

    def test_a_worker_still_running_counts_as_work_through_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracker.md"
            path.write_text(_tracker(
                f"| Upload size limit | Cap uploads | unassigned | open | {TODAY} |  | M | build | implement | #109 | c |\n",
                "| Upload size limit | `src/upload/` |\n"))
            _launch(path, "Cap uploads", "impl-1", ATTEMPT_TREE, "00:41", None, "human_review")
            tracker = path.read_text()
        self.assertEqual(worked_line(open_page(tracker, OPEN_NOW, day=TODAY)),
                         "Worked 1h00m of 1h00m elapsed · 0m waiting")
        self.assertEqual(worked_line(open_page(tracker, OPEN_LATER, day=TODAY)),
                         "Worked 2h00m of 2h00m elapsed · 0m waiting")

    def test_a_close_after_the_worker_ends_includes_the_wait_and_then_freezes(self):
        tracker = ended_attempts((("17:16", "17:17", "human_review"),))
        closed = replace(ISSUE, state="closed", closed_at=at(YESTERDAY, "20:00").astimezone(timezone.utc))
        line = "Worked 1m of 2h44m elapsed · 2h43m waiting"
        self.assertEqual(worked_line(open_page(tracker, OPEN_NOW, issue=closed)), line)
        self.assertEqual(worked_line(open_page(tracker, OPEN_LATER, issue=closed)), line)

    def test_a_task_with_no_launches_has_no_worked_line(self):
        tracker = _tracker(
            f"| Upload size limit | Cap uploads | unassigned | open | {YESTERDAY} |  | M | build | implement | #109 | c |\n",
            "", "- 17:00 stage: Upload size limit → implement\n")
        self.assertNotIn("Worked", flow_section(open_page(tracker, OPEN_NOW)))


class DoneClockAfterMidnightTest(unittest.TestCase):
    """(#213) The task page's elapsed and waiting totals stop at the done clock's own latest occurrence at
    or before now, not at now itself once now's date has turned over past the clock's hour."""

    def test_a_done_clock_the_previous_evening_stops_elapsed_there_not_at_now(self):
        html = midnight_page("22:21", at(TODAY, "00:05"))
        flow = flow_section(html)
        self.assertRegex(flow, r"(?i)\bWorked\s+2h10m\s+of\s+2h16m\s+elapsed\s*·\s*6m\s+waiting\b", flow)

    def test_a_done_clock_earlier_today_still_resolves_to_today(self):
        # Regression guard: just after midnight, a clock before now's own time of day is still today.
        html = midnight_page("00:03", at(TODAY, "00:05"))
        flow = flow_section(html)
        self.assertRegex(flow, r"(?i)\bWorked\s+2h10m\s+of\s+3h58m\s+elapsed\s*·\s*1h48m\s+waiting\b", flow)


if __name__ == "__main__":
    unittest.main()

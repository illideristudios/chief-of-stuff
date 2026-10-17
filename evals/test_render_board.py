"""Unit tests for scripts/render_board.py: the board is a render of the tracker, and every bar cites a field."""

import contextlib
import hashlib
import http.server
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from evals.rules_text import rules_text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import estimate  # noqa: E402
import md  # noqa: E402
import render_board as rb  # noqa: E402
from workspace import parse_coordinator  # noqa: E402
from test_board_flow_design import outline  # noqa: E402
import tracker as tr  # noqa: E402
from backlog import Backlog, GitHubBacklog  # noqa: E402
import settings as st  # noqa: E402

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 16, 14, 30, tzinfo=CT)

# Shaped like a live task whose item cell accumulated its history (anonymised).
LONG_ITEM = (
    "Service architecture refactor (SOLID / clean architecture): rules engine → rule evaluation → service surface; "
    "record fetch tools → service tools; bullets 0–2b landed in the worktree, uncommitted; the worker left the registry at 07:46; "
    "coordinator reassigned the task twice overnight; plan approved 01:20 (relayed, not verbatim); remaining: bullets 3–6, docs pass, "
    "eval rerun, merge to main after review; blocked on nothing but an owner"
)

CLAUDE_MD = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Daily log template: `templates/daily.md`; tracker template `templates/tracker.md`
- Tracker: `daily/<date>-tracker.md` (sections: Tasks, Decisions, File ownership, Log)
- Timezone: America/Chicago
- Calendar tool: `mcp__calendar__list_events`
- Calendars:
  - `cal-work`: work
- Deadlines:
  - Launch: 2026-09-16 23:59
  - Final: 2026-09-20 12:00
- Board: `mcp__board__publish`; URL board-7
- Human-only actions: spending money
"""

TRACKER = """# Tracker 2026-09-16

Coordinator: coordinator. Board: board-7.

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Write eval README | Robin | open | 09:00 | 17:00 | Checklist: Write eval README |
| Security audit | subagent | running 10:30 |  |  | Checklist: Security audit |
| Review deploy config | coordinator | open | 2026-09-15 | Final | Checklist: Review deploy config |
| Draft release notes | Robin | done 11:15 | 09:00 |  | Checklist: Draft release notes |
| Short row | Robin |
| Long | Robin | open | 09:00 | 10:00 | x | extra | cells |
| Ship docs site | Robin | open | 2026-09-16 | 2026-09-18 15:00 | Checklist: Ship docs site |
| LONG_ITEM | worker-9a | waiting | 2026-09-16 |  | Checklist: refactor |

## Decisions

| time | item | Robin's words |
|---|---|---|
| 11:15 | Draft release notes | "done" |

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| a1b2c3 | worker-9a | working | refactor of the loader | | 15:00 | TDD | 2: rules-audit (working), fixture-sweep (idle) | 11:40 |
| d4e5f6 | 7719-fixer | waiting | nothing; S1 landed | Robin — review of S1 | now | | none | 04:45 |
|  | wt-docs | | spawned, not registered | | | | | 11:58 |
| 9f2a41 | 4821-audit | idle | ready for decommissioning 11:20 | | now | | none | 11:20 |
| b7c8d9 | odd-one | dancing | who knows | | | | | 11:00 |

## File ownership

## Log

- 09:00 opened the day
- 11:15 release notes done
""".replace("LONG_ITEM", LONG_ITEM)

LOG = """# 2026-09-16

## Goal

## Calendar

- 09:00–09:15 CDT Standup (work)
- 13:00-14:00 Class: Data modeling (program)
- Deadline: Launch 23:59 CDT
- not a time line
| 15:00 | 15:30 | Table meeting |

## Checklist

- [ ] Write eval README
"""


class ConfigTest(unittest.TestCase):
    def test_parses_coordinator_block(self) -> None:
        cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.assertEqual(cfg.user, "Robin")
        self.assertEqual(cfg.log_dir, "daily/")
        self.assertEqual(cfg.tracker_path("2026-09-16"), "daily/2026-09-16-tracker.md")
        self.assertEqual(cfg.tz, "America/Chicago")
        self.assertEqual([d.name for d in cfg.deadlines], ["Launch", "Final"])
        self.assertEqual(cfg.deadlines[0].at, datetime(2026, 9, 16, 23, 59, tzinfo=CT))
        self.assertEqual(cfg.deadlines[1].at, datetime(2026, 9, 20, 12, 0, tzinfo=CT))
        self.assertEqual(cfg.board_tool, "mcp__board__publish")
        self.assertEqual(cfg.board_url, "board-7")

    def test_missing_block_raises(self) -> None:
        with self.assertRaises(rb.ConfigError):
            parse_coordinator("# Workspace\n\nnothing here\n", today=NOW.date())

    def test_board_line_without_url(self) -> None:
        cfg = parse_coordinator(CLAUDE_MD.replace("- Board: `mcp__board__publish`; URL board-7", "- Board: Artifact"), today=NOW.date())
        self.assertEqual(cfg.board_tool, "Artifact")
        self.assertIsNone(cfg.board_url)


class TrackerParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.t = rb.parse_tracker(TRACKER)

    def test_tasks_and_header(self) -> None:
        self.assertEqual(self.t.board_url, "board-7")
        self.assertEqual([task.item for task in self.t.tasks][:4], ["Write eval README", "Security audit", "Review deploy config", "Draft release notes"])
        self.assertEqual(self.t.tasks[1].state, "running 10:30")
        self.assertEqual(self.t.tasks[2].since, "2026-09-15")
        self.assertEqual(self.t.tasks[2].due, "Final")

    def test_malformed_rows_kept_with_warning(self) -> None:
        short, long = self.t.tasks[4], self.t.tasks[5]
        self.assertEqual(short.item, "Short row")
        self.assertIn("2 cells", short.warning)
        self.assertEqual(long.item, "Long")
        self.assertIn("8 cells", long.warning)


class SessionParseTest(unittest.TestCase):
    """`## Sessions` reaches the board for the first time. Nothing read it before this."""

    def setUp(self) -> None:
        self.s = {x.name: x for x in rb.parse_tracker(TRACKER).sessions}

    def test_it_reads_a_row_into_its_columns(self) -> None:
        w = self.s["worker-9a"]
        self.assertEqual((w.ref, w.state, w.free_at, w.last_reply), ("a1b2c3", "working", "15:00", "11:40"))
        self.assertEqual(w.doing, "refactor of the loader")

    def test_a_session_with_no_ref_is_starting_rather_than_stateless(self) -> None:
        """`:188` — a row that has never carried a ref is not gone, it is not there yet."""
        self.assertEqual(self.s["wt-docs"].kind, "starting")

    def test_ready_is_derived_from_doing_and_never_written(self) -> None:
        """`:189` — `ready for decommissioning` is a `doing` value and never a state of its own."""
        self.assertEqual(self.s["4821-audit"].kind, "ready")

    def test_the_four_written_states_pass_through(self) -> None:
        self.assertEqual(self.s["worker-9a"].kind, "working")
        self.assertEqual(self.s["7719-fixer"].kind, "waiting")

    def test_a_word_outside_the_closed_set_is_kept_and_flagged(self) -> None:
        """Same contract as a malformed Tasks row: keep it, say so, never silently normalise."""
        odd = self.s["odd-one"]
        self.assertEqual(odd.state, "dancing")
        self.assertIn("dancing", odd.warning)

    def test_who_it_waits_on_is_an_edge_not_part_of_the_state(self) -> None:
        """`waiting on` already carries `Zach — A2, A3, B`: the target, then the reason."""
        f = self.s["7719-fixer"]
        self.assertEqual(f.waits_on, "Robin")
        self.assertEqual(f.waits_for, "review of S1")

    def test_a_session_waiting_on_nobody_named_has_no_edge(self) -> None:
        self.assertEqual(self.s["worker-9a"].waits_on, "")

    def test_children_are_counted_and_named(self) -> None:
        w = self.s["worker-9a"]
        self.assertEqual(w.child_names, ("rules-audit", "fixture-sweep"))
        self.assertEqual(self.s["7719-fixer"].child_names, ())

    def test_nothing_reported_is_not_the_same_as_a_word_nobody_knows(self) -> None:
        """Found against the live tracker: four seven-column rows all read `unknown`, which is two facts."""
        self.assertEqual(self.s["wt-docs"].kind, "starting")
        self.assertEqual(self.s["odd-one"].kind, "unknown")
        blank = rb.parse_tracker(
            "# T\n\n## Sessions\n\n| ref | name | state | doing | waiting on | free at | constraints | children | last reply |\n"
            "|---|---|---|---|---|---|---|---|---|\n| a1b2c3 | quiet |  | something |  |  |  |  | 09:00 |\n").sessions[0]
        self.assertEqual(blank.kind, "unreported")

    def test_waiting_on_nobody_is_not_an_edge_to_a_node_called_nothing(self) -> None:
        """The live tracker says `nothing; the stack-default fix landed`. That is not a session."""
        for cell in ("nothing", "none", "-", "—", "nobody", "Nothing; the fix landed"):
            s = tr.Session(ref="a", name="n", state="idle", doing="", waiting_on=cell,
                           free_at="", constraints="", children="", last_reply="")
            self.assertEqual(s.waits_on, "", cell)

    def test_a_comma_separates_who_from_why_when_no_dash_does(self) -> None:
        """Live: `Zach, in that session` — three sessions wrote this cell and did not agree on a separator."""
        s = tr.Session(ref="a", name="n", state="waiting", doing="", waiting_on="Zach, in that session",
                       free_at="", constraints="", children="", last_reply="")
        self.assertEqual(s.waits_on, "Zach")
        self.assertEqual(s.waits_for, "in that session")

    def test_the_node_label_drops_the_history_the_name_cell_accretes(self) -> None:
        """Live: `update-claude-md-docs (third name, same ref)`. The graph wants the name, not its provenance."""
        s = tr.Session(ref="a", name="update-claude-md-docs (third name, same ref)", state="idle", doing="",
                       waiting_on="", free_at="", constraints="", children="", last_reply="")
        self.assertEqual(s.label, "update-claude-md-docs")

    def test_a_tracker_with_no_sessions_section_parses_to_nothing(self) -> None:
        self.assertEqual(rb.parse_tracker("# T\n\n## Tasks\n").sessions, ())

    def test_the_seven_column_shape_still_parses(self) -> None:
        """Every tracker written before this version has no `state` and no `children` column."""
        old = ("# T\n\n## Sessions\n\n| ref | name | doing | waiting on | free at | constraints | last reply |\n"
               "|---|---|---|---|---|---|---|\n| a1b2c3 | old-shape | refactor | Robin — a yes | now | TDD | 09:00 |\n")
        s = rb.parse_tracker(old).sessions[0]
        self.assertEqual((s.ref, s.name, s.doing, s.last_reply), ("a1b2c3", "old-shape", "refactor", "09:00"))
        self.assertEqual(s.state, "")
        self.assertEqual(s.waits_on, "Robin")


class BarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.tasks = {task.item: task for task in rb.parse_tracker(TRACKER).tasks}

    def bar(self, item: str) -> estimate.Bar:
        return rb.day_bar(self.tasks[item], self.cfg, NOW)

    def test_due_time_closes_the_bar(self) -> None:
        b = self.bar("Write eval README")
        self.assertEqual((b.start_src, b.end_src), ("since", "due"))
        self.assertEqual(b.start, datetime(2026, 9, 16, 9, 0, tzinfo=CT))
        self.assertEqual(b.end, datetime(2026, 9, 16, 17, 0, tzinfo=CT))
        self.assertIsNone(b.label)

    def test_running_state_is_the_start_and_no_due_is_open_to_deadline(self) -> None:
        b = self.bar("Security audit")
        self.assertEqual((b.start_src, b.end_src), ("state", "deadline"))
        self.assertEqual(b.start, datetime(2026, 9, 16, 10, 30, tzinfo=CT))
        self.assertEqual(b.end, self.cfg.deadlines[0].at)
        self.assertEqual(b.label, "no estimate")

    def test_carried_task_starts_at_axis_and_due_may_name_a_deadline(self) -> None:
        b = self.bar("Review deploy config")
        self.assertEqual(b.start_src, "carried")
        self.assertEqual(b.end_src, "due")
        self.assertEqual(b.end, self.cfg.deadlines[1].at)

    def test_done_time_closes_the_bar(self) -> None:
        b = self.bar("Draft release notes")
        self.assertEqual(b.end_src, "state")
        self.assertEqual(b.end, datetime(2026, 9, 16, 11, 15, tzinfo=CT))


MIDNIGHT_TRACKER = """# Tracker 2026-09-17

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Night owl | Robin | done 22:21 | 2026-09-16 |  | Checklist: Night owl |
| Early bird | Robin | done 00:03 | 2026-09-17 |  | Checklist: Early bird |

## Log

- 21:00 opened the day
"""


class DoneClockAfterMidnightTest(unittest.TestCase):
    """(#213) A done task's `HH:MM` clock names its latest occurrence at or before now, so the board's
    done bar cannot run past a clock that hasn't happened yet today."""

    def setUp(self) -> None:
        self.now = datetime(2026, 9, 17, 0, 5, tzinfo=CT)
        self.cfg = parse_coordinator(CLAUDE_MD, today=self.now.date())
        self.by = {t.item: t for t in rb.parse_tracker(MIDNIGHT_TRACKER).tasks}

    def test_a_clock_later_than_nows_time_of_day_names_yesterday(self) -> None:
        b = rb.day_bar(self.by["Night owl"], self.cfg, self.now)
        self.assertEqual(b.end_src, "state")
        self.assertEqual(b.end, datetime(2026, 9, 16, 22, 21, tzinfo=CT))
        self.assertLessEqual(b.end, self.now)

    def test_a_clock_earlier_than_nows_time_of_day_still_names_today(self) -> None:
        # Regression guard: just after midnight, a clock before now's own time of day is still today.
        b = rb.day_bar(self.by["Early bird"], self.cfg, self.now)
        self.assertEqual(b.end, datetime(2026, 9, 17, 0, 3, tzinfo=CT))


# A task is drawn on the board only when it has a lane and a stage (#350), so a test that reads a task back
# off the page gives the row both.
LANED_HEAD = "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n|---|---|---|---|---|---|---|---|---|---|---|\n"
LANED_TRACKER = (
    "# Tracker 2026-09-16\n\nCoordinator: coordinator. Board: board-7.\n\n## Tasks\n\n" + LANED_HEAD
    + "| Write eval README | Write eval README | Robin | open | 09:00 | 17:00 | S | build | implement |  | Checklist: Write eval README |\n"
    + "\n## Decisions\n\n## Log\n\n- 09:00 opened the day\n")


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACKER, self.cfg, NOW)

    def test_the_page_declares_its_charset_first(self) -> None:
        """The Artifact wrapper sets one; a local open of the written file showed `Â·` for every ` · ` (C7's visual check)."""
        self.assertTrue(self.html.startswith('<meta charset="utf-8">\n'), self.html[:80])

    def test_embeds_tracker_hash_and_instants(self) -> None:
        sha = hashlib.sha256(TRACKER.encode()).hexdigest()
        self.assertIn(f'<meta name="tracker-sha256" content="{sha}">', self.html)
        self.assertIn('data-rendered-at="2026-09-16T14:30:00-05:00"', self.html)
        self.assertIn('data-deadline="2026-09-16T23:59:00-05:00"', self.html)
        self.assertIn("Launch", self.html)


    def test_no_header_prints_a_rendered_clock(self) -> None:
        # #215: pages re-render on every request, so a render clock only ever says now. The meta keeps the
        # sources' read times and the page's own counts; `data-rendered-at` stays for scripts.
        self.assertNotRegex(self.html, r"\brendered \d{1,2}:\d{2}")
        self.assertIn("tracker 14:30", self.html)
        self.assertIn('data-rendered-at="2026-09-16T14:30:00-05:00"', self.html)

    def test_escapes_html(self) -> None:
        html = rb.render(LANED_TRACKER.replace("Write eval README", "Write <b>README</b> & more"), self.cfg, NOW)
        self.assertNotIn("<b>README</b>", html)
        self.assertIn("&lt;b&gt;README&lt;/b&gt; &amp; more", html)

    def test_deterministic(self) -> None:
        self.assertEqual(self.html, rb.render(TRACKER, self.cfg, NOW))

    def test_an_impossible_clock_or_date_in_a_cell_does_not_stop_the_board(self) -> None:
        # `24:00`, `9:75` or `2026-02-30` in since or due raised ValueError, and the board never re-rendered.
        for bad in ("24:00", "9:75", "2026-02-30", "2026-09-16 25:00"):
            with self.subTest(cell=bad):
                html = rb.render(LANED_TRACKER.replace("| 09:00 | 17:00 |", f"| {bad} | {bad} |", 1), self.cfg, NOW)
                self.assertIn("Write eval README", html)


class ShortNameTest(unittest.TestCase):
    def test_short_name(self) -> None:
        cases = {
            "Service architecture refactor (SOLID / clean architecture): rules engine → rule evaluation": "Service architecture refactor",
            "Fix 5 (FIXES-REQUIRED.md): statement describes cutoffs by recorded sex": "Fix 5 (FIXES-REQUIRED.md)",
            "13 static-analysis errors in fix 4 launch code (Grant.php, launch.php, GrantTest.php), out of TB17": "13 static-analysis errors in fix 4 launch code",
            "Stored, not sent (Robin 20:49 Wed): ARCHITECTURE.md point 2, no finding for some patients": "Stored, not sent (Robin 20:49 Wed)",
            "Fix: login bug": "Fix: login bug",
            "AI cost analysis": "AI cost analysis",
            "": "",
        }
        for item, want in cases.items():
            self.assertEqual(tr.short_name(item), want, item)
        # The width belongs to the medium that has one. `short_name` returns the name; `clip_name`
        # is what a caller one terminal line wide calls, and the cut lives there now.
        long_item = "a" * 30 + " " + "b" * 30 + " " + "c" * 30
        self.assertEqual(long_item, tr.short_name(long_item))
        self.assertEqual("a" * 30 + "…", tr.clip_name(long_item))
        for item in cases:
            self.assertLessEqual(len(tr.clip_name(item)), 49)

    def test_a_long_bolded_item_keeps_its_name(self) -> None:
        """A cut inside a lone `**` span must not eat the whole name.

        The trim to a whole number of mark spans uses `rfind("**")`, and an item whose only opening
        `**` is at index 0 trims to the empty string — so the name comes back as a bare `…`, which
        is not a short name, it is the absence of one. `render_board.py:395` (`label or
        short_name(item)`) puts that straight on the board as a task called `…`.
        """
        item = "**ARCHITECTURE.md section 12.5 rewritten so the deploy story matches the code**"
        got = tr.clip_name(item)
        self.assertNotEqual("…", got)
        self.assertIn("ARCHITECTURE.md", got)
        self.assertLessEqual(len(got), 49)
        self.assertTrue(tr._whole(got.rstrip("…")), got)

    def test_every_cut_still_leaves_no_orphan_mark(self) -> None:
        for item in ("**" + "a" * 30 + " " + "b" * 30 + "**",
                     "lead in **" + "c" * 60 + "** trailing",
                     "**short**",
                     "**a** and **" + "d" * 60 + "**"):
            self.assertTrue(tr._whole(tr.clip_name(item).rstrip("…")), item)
            self.assertNotEqual("…", tr.clip_name(item), item)


CLAUDE_MD_REQ = CLAUDE_MD.replace("  - Launch: 2026-09-16 23:59", "  - Launch: 2026-09-16 23:59; requirements `daily/launch-reqs.md`").replace(
    "  - Final: 2026-09-20 12:00", "  - Final: 2026-09-20 12:00; requirements `daily/final-reqs.md`"
)

REQS = """# Final requirements

Distilled from the brief. This line is a note, not an item.

## Submission

- [x] Deployed URL in the submission — evidence: https://example.test (Log 09:12)
- [ ] Demo video, 3–5 min
  - [ ] Shows the <agent> answering a multi-turn question
- [x] Social post — evidence: commit abc1234

## Engineering

- [ ] Eval suite in CI
- [ ] Cost analysis at 100/1K/10K/100K users
* [ ] star bullets are not items
"""


class RequirementsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD_REQ, today=NOW.date())

    def render(self, now: datetime = NOW, final: str | None = REQS, launch: str | None = None) -> str:
        return rb.render(TRACKER, self.cfg, now, requirements={"Final": final, "Launch": launch})

    def test_deadline_names_requirements_file(self) -> None:
        launch, final = self.cfg.deadlines
        self.assertEqual(final.at, datetime(2026, 9, 20, 12, 0, tzinfo=CT))
        self.assertEqual(final.requirements, "daily/final-reqs.md")
        self.assertEqual(launch.requirements, "daily/launch-reqs.md")
        plain = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.assertEqual([d.requirements for d in plain.deadlines], [None, None])

    def test_parse_requirements(self) -> None:
        groups = rb.parse_requirements(REQS)
        self.assertEqual([g for g, _ in groups], ["Submission", "Engineering"])
        sub = groups[0][1]
        self.assertEqual([(r.text, r.done, r.depth) for r in sub], [
            ("Deployed URL in the submission", True, 0),
            ("Demo video, 3–5 min", False, 0),
            ("Shows the <agent> answering a multi-turn question", False, 1),
            ("Social post", True, 0),
        ])
        self.assertEqual(sub[0].evidence, "https://example.test (Log 09:12)")
        self.assertEqual(sub[1].evidence, "")
        self.assertEqual(len(groups[1][1]), 2)


    def test_cli_reads_files_and_summarises(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD_REQ)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(TRACKER)
        (root / "daily" / "final-reqs.md").write_text(REQS)
        buf = io.StringIO()

        class Frozen(datetime):  # the Final deadline is 2026-09-20; the wall clock passed it
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz)

        with mock.patch.object(rb, "datetime", Frozen), contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertIn("requirements=Final:2/6", buf.getvalue())
        self.assertIn("requirements_missing=1", buf.getvalue())


LIGHT = {"--bg": "#F4EFE1", "--surface": "#FBF8EF", "--fg": "#2F2630", "--muted": "#6E6470", "--line": "#DDD3BD", "--brass": "#A7843E", "--dl": "#C9533A"}
DARK = {"--bg": "#1C1813", "--surface": "#262019", "--fg": "#EFE6D2", "--muted": "#B3A791", "--line": "#3D352A", "--brass": "#D4B06A", "--dl": "#F08566"}


class BrandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACKER, self.cfg, NOW)
        self.css = self.html.split("<style>", 1)[1].split("</style>", 1)[0]


    def test_palette_tokens(self) -> None:
        root = re.search(r"^:root\{(.*?)\}", self.css, re.M | re.S).group(1)
        for name, hex_ in LIGHT.items():
            self.assertIn(f"{name}:{hex_.lower()}", root.lower(), name)
        for block in (r'@media \(prefers-color-scheme:dark\)\{:root:not\(\[data-theme="light"\]\)\{(.*?)\}\}', r':root\[data-theme="dark"\]\{(.*?)\}'):
            body = re.search(block, self.css, re.S).group(1).lower()
            for name, hex_ in DARK.items():
                self.assertIn(f"{name}:{hex_.lower()}", body, f"{name} in {block[:30]}")
        used = set(re.findall(r"var\((--[a-z-]+)\)", self.css))
        theme = set(re.findall(r"(--[a-z-]+):", root))
        defined = theme | set(re.findall(r"(--[a-z-]+):", self.css))
        self.assertTrue(used <= defined, used - defined)
        self.assertTrue(set(LIGHT) <= theme, set(LIGHT) - theme)

    def test_fonts_with_fallbacks(self) -> None:
        link = re.search(r'<link rel="stylesheet" href="(https://fonts\.googleapis\.com/css2\?[^"]+)">', self.html)
        self.assertIsNotNone(link)
        self.assertIn("Cormorant+SC", link.group(1))
        self.assertIn("Alegreya+Sans", link.group(1))
        # The vendored canonical theme satisfies this convention inside its own `--f-*` tokens and refers to
        # them (`font-family: var(--f-head)`); its bytes are pinned by hash in test_page_theme, so the scan
        # below applies to the page's own rules, which follow the theme's end marker.
        own_css = self.css.split("the canonical theme ends here", 1)[-1]
        families = re.findall(r"font(?:-family)?:([^;}]+)", own_css)
        self.assertTrue(families)
        for f in families:
            self.assertRegex(f.strip(), r"(serif|sans-serif|monospace)$", f)


class OrphanAndUnassignedTest(unittest.TestCase):
    TRACKER = TRACKER.replace(
        "| Security audit | subagent | running 10:30 |  |  | Checklist: Security audit |",
        "| Security audit | 4821-audit | orphaned | 09:00 | 17:00 | Checklist: Security audit |\n| Pick a deploy window | unassigned | open | 09:00 |  | Checklist: deploy window |",
    )

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(self.TRACKER, self.cfg, NOW)

    def test_orphaned_is_an_active_kind(self) -> None:
        task = next(l for l in rb.parse_tracker(self.TRACKER).tasks if l.item == "Security audit")
        self.assertEqual(task.kind, "orphaned")


class PhoneTest(unittest.TestCase):
    """Stage 6: the page at 390px, which is where it is read in the morning.

    Laptop first was the decision, so nothing here changes the wide layout. What it fixes is the
    three places that cannot survive a 390px viewport: the due row's fixed owner and time columns,
    the strip's percentage name column (36% of 390 is 140px of name against 218px of axis), and the
    six-column task table, which is wider than the screen whatever the CSS says.

    A table is allowed to scroll sideways inside its own box. The page body is not — a board that
    slides under the thumb is the failure this stage exists to prevent.
    """

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACKER, self.cfg, NOW)
        self.css = self.html.split("<style>")[1].split("</style>")[0]
        at = self.css.index("@media (max-width:420px)")
        self.phone = self.css[at:]

    def test_there_is_a_phone_block_at_all(self) -> None:
        self.assertRegex(self.css, r"@media \(max-width:420px\)")


    def test_nothing_forces_the_body_wider_than_a_phone(self) -> None:
        """A min-width wider than the screen is the usual way a page starts sliding sideways."""
        for m in re.finditer(r"min-width:(\d+)px", self.css):
            self.assertLessEqual(int(m.group(1)), 390, m.group(0))


def claude_md_pages(port: int) -> str:
    return re.sub(r"(?m)^- Board: .*$", f"- Board: self-hosted; URL http://127.0.0.1:{port}/; dir `pages/`", CLAUDE_MD)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CliTest(unittest.TestCase):
    def test_writes_board_beside_tracker(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(TRACKER)
        (root / "daily" / "2026-09-16.md").write_text(LOG)
        out = rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertEqual(out, root / "daily" / "2026-09-16-board.html")
        self.assertTrue(out.exists())
        self.assertIn('name="tracker-sha256"', out.read_text())

    def pages_root(self, port: int) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(claude_md_pages(port))
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(TRACKER)
        return root

    def test_a_board_dir_takes_the_board(self) -> None:
        # Zach, 2026-09-24 14:07: "we should host our own webserver"; the board goes where pages.py serves.
        port = free_port()
        root = self.pages_root(port)
        buf = io.StringIO()
        with mock.patch("pages.ensure", side_effect=AssertionError("render started the server")), \
             contextlib.redirect_stdout(buf):
            out = rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertEqual(out, root / "pages" / "2026-09-16-board.html")
        page = out.read_text()
        # The reload check is injected by pages.py at serve time (one shared snippet, #330), not rendered into the file.
        for poll in ("fetch(location.pathname", "Last-Modified", "document.lastModified", "visibilitychange"):
            self.assertNotIn(poll, page)
        # The serve-time injection lands before the last `</body>`; a board without one is served with the snippet appended.
        self.assertIn("<body", page)
        self.assertIn("</body>", page)
        self.assertFalse((root / "pages" / ".pid").exists())
        self.assertNotIn("http://127.0.0.1", buf.getvalue())

    def test_a_render_only_writes_even_when_board_port_is_occupied(self) -> None:
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        root = self.pages_root(other.server_address[1])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertTrue(out.exists())
        self.assertNotIn("pages:", buf.getvalue())

    def test_the_board_writes_a_decisions_page_beside_it(self) -> None:
        # Zach, 2026-09-26 00:50: "a decisions page that automatically links to each pending decision and shows
        # completed decisions". A decision is answered once a Decisions row, on any day, names its page.
        root = self.pages_root(free_port())
        pages = root / "pages"
        pages.mkdir()
        ask = {"ask": "Which window?", "options": [{"key": "A", "title": "Tonight"}, {"key": "B", "title": "Monday"}],
               "recommended": "B", "why": "Quiet.", "default": "Nothing ships."}
        for n, slug in enumerate(("deploy-window", "uploads-storage", "cache-size")):
            (pages / f"decision-{slug}.json").write_text(json.dumps(dict(ask, headline=f"Decide {slug}")))
            (pages / f"decision-{slug}.html").write_text("page")
            os.utime(pages / f"decision-{slug}.json", (1000 + n, 1000 + n))
        (pages / "decision-broken.json").write_text("{")
        os.utime(pages / "decision-broken.json", (999, 999))
        (root / "daily" / "2026-09-15-tracker.md").write_text(
            "# Tracker\n\n## Decisions\n\n| time | item | Robin's words |\n|---|---|---|\n"
            "| 17:00 | option A of decision-uploads-storage | \"A\" |\n")
        tracker = TRACKER.replace('| 11:15 | Draft release notes | "done" |',
                                  '| 11:15 | Draft release notes | "done" |\n| 11:20 | option B of decision-cache-size | "B, go" |')
        (root / "daily" / "2026-09-16-tracker.md").write_text(tracker)
        with contextlib.redirect_stdout(io.StringIO()):
            board = rb.main(["--date", "2026-09-16", "--root", str(root)]).read_text()
        page = (pages / "decisions.html").read_text()
        pending, completed = page.split("COMPLETED", 1)
        self.assertRegex(pending, r'(?s)href="/decisions/deploy-window".*decision-broken')
        for text in ("Decide deploy-window", "Which window?", "Monday", "Nothing ships."):
            self.assertIn(text, pending)
        self.assertNotIn("uploads-storage", pending)
        self.assertNotIn("cache-size", pending)
        self.assertIn("Draft release notes", completed)
        self.assertRegex(completed, r'(?s)11:20.*href="/decisions/cache-size".*B, go')
        # The unreadable file is listed, and counted apart from the pending ones (Decisions.dc.html: "1 unreadable").
        self.assertIn("1 unreadable", page)
        # Each page is rendered again from its JSON and the tracker: the answered one wears its answer.
        self.assertIn(">ANSWERED · B<", (pages / "decision-cache-size.html").read_text())
        self.assertIn(">PENDING<", (pages / "decision-deploy-window.html").read_text())
        self.assertEqual((pages / "decision-broken.html").exists(), False)
        # The board's DECISIONS panel lists the same pending entries as decisions.html, the unreadable one apart.
        self.assertIn('<a class="panels-link" href="/decisions">decisions page</a><b class="panels-count">1</b>', board)
        self.assertIn('<a class="panels-name" href="/decisions/deploy-window">Decide deploy-window</a>', board)
        # #349: the artboard's label. Its six tiles have no DECISIONS; the pending count is the tab bar's badge.
        self.assertNotIn('<span class="panels-label">DECISIONS</span>', board)
        self.assertIn('<span class="badge" title="1 pending">1</span>', board)
        self.assertIn('decision-broken.json', board)

    def test_summary_line_counts_long_items(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(TRACKER)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertIn("long_items=1", buf.getvalue())

    def test_missing_tracker_is_an_error(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        with self.assertRaises(SystemExit):
            rb.main(["--date", "2026-09-16", "--root", str(root)])


RESUME = """- As of: 09:52 — gauntlet-f0 [308833]
- In flight: nothing; the poll to 4821-audit is out
- Next: read 4821-audit's reply, then the deploy decision
- Waiting on: Robin — TB17 diff review
- Re-arm: the 30-minute check; the board watch
- Verified: origin/main d6aab3b, 2 unpushed; agent /ready 200, login 200 (09:47)
"""
TRACKER_RESUME = TRACKER.replace("## Tasks", "## Resume\n\n" + RESUME + "\n## Tasks", 1)


class ResumeBlockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())

    def test_parses_the_block_into_fields(self) -> None:
        block = rb.parse_resume(TRACKER_RESUME)
        self.assertEqual(block["as of"], "09:52 — gauntlet-f0 [308833]")
        self.assertEqual(block["waiting on"], "Robin — TB17 diff review")
        self.assertIn("origin/main d6aab3b", block["verified"])

    def test_no_block_is_empty_not_an_error(self) -> None:
        self.assertEqual(rb.parse_resume(TRACKER), {})

    def test_board_url_still_comes_from_the_header_not_the_block(self) -> None:
        # A block above Tasks is inside the old header slice; a `Board:` word in it must not win.
        tracker = TRACKER.replace(
            "## Tasks",
            "## Resume\n\n- Next: say the Board: not-a-url line was never republished\n\n## Tasks",
            1,
        )
        self.assertEqual(rb.parse_tracker(tracker).board_url, "board-7")

    def test_a_tracker_with_no_header_url_still_reads_none(self) -> None:
        tracker = TRACKER.replace("Coordinator: coordinator. Board: board-7.", "Coordinator: coordinator.")
        self.assertIsNone(rb.parse_tracker(tracker).board_url)

    def test_a_block_cannot_supply_the_url_when_the_header_has_none(self) -> None:
        # The header is what precedes the first `## `, so no later section can name the board.
        tracker = TRACKER_RESUME.replace("Coordinator: coordinator. Board: board-7.", "Coordinator: coordinator.")
        tracker = tracker.replace("- Re-arm:", "- Note: Board: bogus-url was last published at 09:00\n- Re-arm:")
        self.assertIsNone(rb.parse_tracker(tracker).board_url)

    def test_the_block_does_not_become_a_task(self) -> None:
        self.assertEqual(len(rb.parse_tracker(TRACKER_RESUME).tasks), len(rb.parse_tracker(TRACKER).tasks))


    def test_summary_counts_long_resume_lines(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        long_line = "- Next: " + "a merge order decision that will not fit on one line " * 6
        tracker = TRACKER_RESUME.replace("- Next: read 4821-audit's reply, then the deploy decision", long_line)
        (root / "daily" / "2026-09-16-tracker.md").write_text(tracker)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertIn("resume_long=1", buf.getvalue())

    def test_summary_reports_no_block(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(TRACKER)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(root)])
        self.assertIn("resume=none", buf.getvalue())


if __name__ == "__main__":
    unittest.main()


class ResumeLegibilityTest(unittest.TestCase):
    """Zach, reading the live board: "That's unintelligible. no formatting."

    Three faults under one complaint, and the one that was reported is the least of them.
    """

    LIVE = """- As of: 16:52 — gauntlet-bc [56e3fd]
- In flight: nothing, and nothing has changed since 13:40
- Next: goal #1 — owners for the three orphaned trees, then deploy main
- Waiting on: Zach — tree owners, redeploy, S2, CLAUDE.md:172, restart
- Re-arm: no Standing list; nothing handed without a fresh yes. Board watch dropped 16:51.
- Verified 16:52: main = origin/main 6621341, unmoved. Both services 200. Audit clean.
"""

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.tracker = TRACKER.replace("## Tasks", "## Resume\n\n" + self.LIVE + "\n## Tasks", 1)
        self.block = rb.parse_resume(self.tracker)

    def test_a_clock_read_in_the_label_stays_out_of_the_key(self) -> None:
        """`- Verified 16:52:` split at the first colon, so the key was `verified 16` and nothing ever matched it."""
        self.assertIn("verified", self.block)
        self.assertNotIn("verified 16", self.block)
        self.assertIn("16:52", self.block["verified"])
        self.assertIn("origin/main 6621341", self.block["verified"])


    def test_an_over_long_field_is_counted_as_a_warning_not_as_telemetry(self) -> None:
        """`resume_long=4` printed beside `warnings=0` for three hours and read as a task count."""
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        long_line = "- Next: " + "a merge order decision that will not fit on one line " * 6
        tracker = self.tracker.replace("- Next: goal #1 — owners for the three orphaned trees, then deploy main", long_line)
        (root / "daily" / "2026-09-16-tracker.md").write_text(tracker)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(root)])
        out = buf.getvalue()
        self.assertIn("resume_long=1", out)
        self.assertNotIn("warnings=0", out)

    def test_a_field_that_obeys_the_written_rule_is_not_warned_about(self) -> None:
        """`RESUME_LINE` did two jobs with one number, and the two jobs disagreed the moment one moved.

        `agents/chief-of-stuff.md` bounds a field at 300 characters. The renderer warned past 160, so
        a block whose every field obeys the rule exactly still printed `resume_long=5` — a guard that
        cannot be satisfied is one a writer learns to ignore, which is how the live `As of` reached
        19 044 characters with the count in every render for nine hours. Folding is a display
        question and warning is a hygiene question; they want their own numbers.

        Measured on a live 136-task tracker: 286, 241, 269, 250, 273 — over the fold, inside the rule.
        """
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD)
        (root / "daily").mkdir()
        obeys = "- Next: " + "x" * 250
        tracker = self.tracker.replace("- Next: goal #1 — owners for the three orphaned trees, then deploy main", obeys)
        (root / "daily" / "2026-09-16-tracker.md").write_text(tracker)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(root)])
        out = buf.getvalue()
        self.assertIn("resume_long=0", out)


class ResumeCountTest(unittest.TestCase):
    """Finding 65, applied to itself: the number on stdout is the number of rows on the page.

    A field written with no value is parsed, counted, and not drawn — which is the same gap that hid
    `re-arm` for a day, one size smaller. The count that belongs on stdout is the one after the filter.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / "CLAUDE.md").write_text(CLAUDE_MD)
        (self.root / "daily").mkdir()
        resume = "- As of: 09:00 — gauntlet-f0\n- In flight:\n- Next: the deploy decision\n"
        self.tracker = TRACKER.replace("## Tasks", "## Resume\n\n" + resume + "\n## Tasks", 1)
        (self.root / "daily" / "2026-09-16-tracker.md").write_text(self.tracker)

    def test_the_reported_count_is_what_was_drawn_and_not_what_was_parsed(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--date", "2026-09-16", "--root", str(self.root)])
        self.assertEqual(len(rb.parse_resume(self.tracker)), 3, "three fields are parsed")
        self.assertIn("resume=2 fields", buf.getvalue())


QUEUE_TRACKER = """# Tracker 2026-09-16

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| A oldest | worker | open | 2026-09-15 |  | c |
| B running | worker [ab12cd] | running 10:00 | 2026-09-16 |  | c |
| C newer | worker (was ab12cd) | open | 2026-09-16 |  | c |
| D dated | worker | open | 2026-09-16 | 17:00 | c |
| E unassigned | unassigned | open | 2026-09-16 |  | c |
| F blank |  | open | 2026-09-16 |  | c |
| G orphaned | left-session | orphaned 09:05 | 2026-09-15 |  | c |
| H left behind | left-session | open | 2026-09-16 |  | c |
| I dash | — | open | 2026-09-16 |  | c |
| J beyond | worker | open | 2026-09-16 | Final | c |
| K done | worker | done 11:00 | 2026-09-16 |  | c |

## Log

- 09:00 opened the day
"""


class EstimateTest(unittest.TestCase):
    """An owned task with no `due` ends where its owner's queue puts it, and cites `derived`.

    The tracker's clock is day-grained (`since` and `due` are dates; only `done` carries a time), so no
    task has a duration to learn from. The estimator therefore claims nothing about effort: task i of
    an owner's N ends at `H - (N-i)/N x (H - now)`, where H is the nearest deadline still ahead. The
    one fabricated input is the order, and it is stated: running first, then oldest first.
    """

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.tasks = [task for task in rb.parse_tracker(QUEUE_TRACKER).tasks if task.kind != "done"]
        self.by = {task.item: task for task in self.tasks}
        self.est = rb.estimates(self.tasks, self.cfg, NOW)
        self.h = self.cfg.deadlines[0].at  # Launch 23:59, the nearest deadline ahead of 14:30
        self.r = self.h - NOW

    def test_the_queue_is_running_first_then_oldest_and_the_dated_task_counts(self) -> None:
        # worker: B (running), A (since 09-15), C (09-16), D (09-16, due 17:00 <= H) -> N=4; J's due is past H.
        self.assertEqual({k for k in self.est if self.by[k].owner.startswith("worker")}, {"A oldest", "B running", "C newer"})
        self.assertEqual((self.est["B running"].i, self.est["B running"].n), (1, 4))
        self.assertEqual((self.est["A oldest"].i, self.est["A oldest"].n), (2, 4))
        self.assertEqual((self.est["C newer"].i, self.est["C newer"].n), (3, 4))
        self.assertEqual(self.est["B running"].end, self.h - self.r * (3 / 4))
        self.assertEqual(self.est["A oldest"].end, self.h - self.r * (2 / 4))
        self.assertEqual(self.est["C newer"].end, self.h - self.r * (1 / 4))
        self.assertEqual(self.est["A oldest"].label, "est. 2/4 → Launch")

    def test_a_ref_and_a_parenthetical_are_one_owner(self) -> None:
        self.assertEqual({self.est[k].n for k in ("A oldest", "B running", "C newer")}, {4})

    def test_the_last_task_ends_at_the_deadline_instant(self) -> None:
        tasks = [self.by["A oldest"], self.by["C newer"]]
        est = rb.estimates(tasks, self.cfg, NOW)
        self.assertEqual((est["A oldest"].i, est["C newer"].i), (1, 2))
        self.assertEqual(est["C newer"].end, self.h)
        self.assertEqual(est["A oldest"].end, NOW + self.r / 2)

    def test_unowned_gone_and_dated_tasks_get_no_estimate(self) -> None:
        for item in ("D dated", "E unassigned", "F blank", "G orphaned", "H left behind", "I dash", "J beyond"):
            self.assertNotIn(item, self.est, item)

    def test_a_derived_end_reaches_the_bar_only_when_asked_for(self) -> None:
        task = self.by["A oldest"]
        plain = rb.day_bar(task, self.cfg, NOW)
        self.assertEqual((plain.end_src, plain.label), ("deadline", "no estimate"))
        b = rb.day_bar(task, self.cfg, NOW, self.est)
        self.assertEqual((b.end_src, b.label, b.end), ("derived", "est. 2/4 → Launch", self.est["A oldest"].end))
        d = rb.day_bar(self.by["D dated"], self.cfg, NOW, self.est)
        self.assertEqual(d.end_src, "due")
        u = rb.day_bar(self.by["E unassigned"], self.cfg, NOW, self.est)
        self.assertEqual((u.end_src, u.label), ("deadline", "no estimate"))

    def test_a_passed_deadline_makes_the_horizon_midnight(self) -> None:
        late = datetime(2026, 9, 21, 9, 0, tzinfo=CT)
        est = rb.estimates([self.by["A oldest"]], self.cfg, late)
        self.assertEqual(est["A oldest"].end, datetime(2026, 9, 22, 0, 0, tzinfo=CT))
        self.assertEqual(est["A oldest"].label, "est. 1/1 → end of day")


    def test_item_text_is_never_read(self) -> None:
        text = QUEUE_TRACKER.replace("| A oldest |", "| A oldest, a very long item whose length says nothing about the work left (history: 09:00 opened; 10:00 blocked; 11:00 resumed; 12:00 more of the same) |")
        tasks = [task for task in rb.parse_tracker(text).tasks if task.kind != "done"]
        other = rb.estimates(tasks, self.cfg, NOW)
        self.assertEqual({e.end for e in other.values()}, {e.end for e in self.est.values()})


SIZED_TRACKER = """# Tracker 2026-09-16

## Tasks

| item | owner | state | since | due | size | checklist |
|---|---|---|---|---|---|---|
| A oldest | worker | open | 2026-09-15 |  | M | c |
| B running | worker [ab12cd] | running 13:00 | 2026-09-16 |  | S | c |
| C newer | worker | open | 2026-09-16 |  |  | c |
| D dated | worker | open | 2026-09-16 | 17:00 | L | c |
| E unassigned | unassigned | open | 2026-09-16 |  | M | c |
| K1 done | worker | done 09:00–10:00 | 2026-09-16 |  | M | c |
| K2 done | worker | done 10:00-12:00 | 2026-09-16 |  | M | c |
| K3 done | other | done 11:00–11:30 | 2026-09-16 |  | S | c |
| K4 done | other | done 08:00–09:00 | 2026-09-16 |  |  | c |
| K5 no range | worker | done 11:00 | 2026-09-16 |  | M | c |
| K6 backwards | worker | done 23:00–01:00 | 2026-09-16 |  | XL | c |

## Log

- 09:00 opened the day
"""


class SizeGrammarTest(unittest.TestCase):
    """The Tasks table may carry a `size` column, and `done` may keep the running start as a range.

    Finding 77: the estimator's second model needs two things the tracker never recorded — how big a
    task is and how long tasks of that size took. The size is the coordinator's judgement in a column;
    the duration is the `running HH:MM` start kept on close, `done 21:16–22:05`, which `done 22:05`
    alone threw away (finding 78).
    """

    def setUp(self) -> None:
        self.t = rb.parse_tracker(SIZED_TRACKER)
        self.by = {task.item: task for task in self.t.tasks}

    def test_seven_columns_parse_and_the_size_is_read_by_header(self) -> None:
        self.assertEqual(self.by["A oldest"].size, "M")
        self.assertEqual(self.by["C newer"].size, "")
        self.assertEqual(self.by["A oldest"].checklist, "c")
        self.assertEqual([task.warning for task in self.t.tasks], [""] * len(self.t.tasks))

    def test_six_columns_still_parse_unsized_without_a_warning(self) -> None:
        t = rb.parse_tracker(QUEUE_TRACKER)
        self.assertEqual({task.size for task in t.tasks}, {""})
        self.assertEqual([task.warning for task in t.tasks], [""] * len(t.tasks))
        self.assertEqual(t.tasks[0].checklist, "c")

    def test_a_row_matching_neither_width_still_warns(self) -> None:
        text = SIZED_TRACKER.replace("| C newer | worker | open | 2026-09-16 |  |  | c |", "| C newer | worker | open |")
        task = {l.item: l for l in rb.parse_tracker(text).tasks}["C newer"]
        self.assertIn("3 cells", task.warning)
        self.assertIn("expected 7", task.warning)

    def test_a_done_range_keeps_the_end_as_state_time_and_exposes_the_start(self) -> None:
        self.assertEqual(self.by["K1 done"].state_time, "10:00")
        self.assertEqual(self.by["K1 done"].ran, ("09:00", "10:00"))
        self.assertEqual(self.by["K2 done"].ran, ("10:00", "12:00"))
        self.assertIsNone(self.by["K5 no range"].ran)
        self.assertEqual(self.by["K5 no range"].state_time, "11:00")
        self.assertEqual(self.by["B running"].state_time, "13:00")
        self.assertIsNone(self.by["B running"].ran)

    def test_a_done_range_draws_the_bar_as_before(self) -> None:
        cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        b = rb.day_bar(self.by["K1 done"], cfg, NOW)
        self.assertEqual((b.end, b.end_src), (datetime(2026, 9, 16, 10, 0, tzinfo=CT), "state"))

    def test_history_means_per_size_with_an_all_sizes_row(self) -> None:
        cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        h = rb.history(self.t.tasks, cfg, NOW)
        self.assertEqual(h["M"], (timedelta(hours=1, minutes=30), 2))
        self.assertEqual(h["S"], (timedelta(minutes=30), 1))
        self.assertEqual(h[""], (timedelta(hours=1), 1))
        self.assertEqual(h[rb.ALL_SIZES], (timedelta(minutes=67, seconds=30), 4))  # 60+120+30+60 over four
        self.assertNotIn("XL", h)  # K6 ran backwards across midnight and is dropped
        self.assertNotIn("L", h)

    def test_no_range_anywhere_means_no_history(self) -> None:
        cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.assertEqual(rb.history(rb.parse_tracker(QUEUE_TRACKER).tasks, cfg, NOW), {})


class SizedEstimateTest(unittest.TestCase):
    """With history, a task ends where its owner's queue puts it times what tasks of its size have taken.

    Zach, 2026-09-18 23:07: "infer a t-shirt size ... generate an average amount of time spent per t-shirt
    sized puzzle ... use that." A cursor starts at now; a running task ends at its own start plus the mean
    for its size, never before now; every other task ends at the cursor plus its mean; a task with a due
    advances the cursor and keeps its due. Only blank-due owned tasks get a derived end.
    """

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.tasks = list(rb.parse_tracker(SIZED_TRACKER).tasks)
        self.active = [task for task in self.tasks if task.kind != "done"]
        self.by = {task.item: task for task in self.tasks}
        self.hist = rb.history(self.tasks, self.cfg, NOW)
        self.est = rb.estimates(self.active, self.cfg, NOW, self.hist)
        self.m, self.s, self.unsized = timedelta(minutes=90), timedelta(minutes=30), timedelta(minutes=67, seconds=30)

    def test_ends_chain_from_now_by_size_means(self) -> None:
        # worker: B running (S, started 13:00), A oldest (M), C newer (unsized -> all-sizes mean), D dated (L, due 17:00, no L history -> all-sizes mean).
        b = self.est["B running"]
        self.assertEqual(b.end, NOW)  # 13:00 + 30m = 13:30 is already past 14:30; never before now
        self.assertEqual(self.est["A oldest"].end, NOW + self.m)
        self.assertEqual(self.est["C newer"].end, NOW + self.m + self.unsized)
        self.assertNotIn("D dated", self.est)

    def test_labels_name_the_size_and_the_mean(self) -> None:
        self.assertEqual(self.est["A oldest"].label, "est. M ~1h30m")
        self.assertEqual(self.est["B running"].label, "est. S ~30m")
        self.assertEqual(self.est["C newer"].label, "est. ? ~1h07m")
        self.assertEqual({e.basis for e in self.est.values()}, {"history"})
        self.assertEqual((self.est["A oldest"].size, self.est["A oldest"].i, self.est["A oldest"].n), ("M", 2, 4))

    def test_the_title_says_what_the_mean_rests_on(self) -> None:
        t = self.est["A oldest"].title("worker")
        self.assertIn("size M", t)
        self.assertIn("2 M tasks", t)
        self.assertIn("2nd of 4", t)
        self.assertIn("worker", t)
        t = self.est["C newer"].title("worker")
        self.assertIn("unsized", t)
        self.assertIn("all 4", t)

    def test_a_size_with_no_history_borrows_the_all_sizes_mean(self) -> None:
        text = SIZED_TRACKER.replace("| C newer | worker | open | 2026-09-16 |  |  | c |", "| C newer | worker | open | 2026-09-16 |  | XL | c |")
        tasks = list(rb.parse_tracker(text).tasks)
        est = rb.estimates([l for l in tasks if l.kind != "done"], self.cfg, NOW, rb.history(tasks, self.cfg, NOW))
        self.assertEqual(est["C newer"].end, NOW + self.m + self.unsized)
        self.assertEqual(est["C newer"].label, "est. XL ~1h07m")
        self.assertIn("no XL task has closed with a range", est["C newer"].title("worker"))

    def test_unowned_tasks_get_nothing_even_when_sized(self) -> None:
        self.assertNotIn("E unassigned", self.est)

    def test_no_history_falls_back_to_the_queue_drain(self) -> None:
        est = rb.estimates(self.active, self.cfg, NOW, {})
        self.assertEqual(est["A oldest"].label, "est. 2/4 → Launch")
        self.assertEqual(est["A oldest"].basis, "queue")
        self.assertEqual(rb.estimates(self.active, self.cfg, NOW), est)


CLAUDE_MD_EXAM = CLAUDE_MD.replace("  - Final: 2026-09-20 12:00", "  - Final: 2026-09-20 12:00\n  - PCCAT 1st attempt: 2026-09-26; named tasks only")
CLAUDE_MD_EXAM_GOVERNS = CLAUDE_MD.replace("  - Final: 2026-09-20 12:00", "  - Final: 2026-09-20 12:00\n  - PCCAT 1st attempt: 2026-09-26")
SUNDAY = datetime(2026, 9, 20, 12, 1, tzinfo=CT)
SUNDAY_TRACKER = """# Tracker 2026-09-20

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Leftover one | worker | open | 2026-09-19 |  | c |
| Leftover two | worker | open | 2026-09-20 |  | c |
| Nobody's | unassigned | open | 2026-09-20 |  | c |
| Study | Zach | open | 2026-09-20 | PCCAT 1st attempt | c |

## Log

- 09:00 opened the day
"""


class DeadlineScopeTest(unittest.TestCase):
    """Finding 73: after Final, the nearest deadline is an exam, and nothing on the Tasks table is due to it.

    A deadline line may say `named tasks only`: tasks whose `due` names it still draw to it, and no task
    falls to it by default. The estimator's horizon, the unowned task's open end and the week strip's
    fold use the nearest deadline that governs unnamed tasks; the Clock line and the axes keep the
    nearest deadline of all, because the exam is real.
    """

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD_EXAM, today=SUNDAY.date())
        self.governs = parse_coordinator(CLAUDE_MD_EXAM_GOVERNS, today=SUNDAY.date())
        self.tasks = list(rb.parse_tracker(SUNDAY_TRACKER).tasks)
        self.by = {task.item: task for task in self.tasks}

    def test_the_option_parses_and_defaults_to_all(self) -> None:
        exam = next(d for d in self.cfg.deadlines if d.name.startswith("PCCAT"))
        self.assertEqual(exam.scope, "named")
        self.assertEqual({d.scope for d in self.cfg.deadlines if d.name != exam.name}, {"all"})
        self.assertEqual({d.scope for d in self.governs.deadlines}, {"all"})
        both = parse_coordinator(CLAUDE_MD.replace("  - Final: 2026-09-20 12:00", "  - Final: 2026-09-20 12:00; named tasks only; requirements `reqs.md`"), today=SUNDAY.date())
        final = next(d for d in both.deadlines if d.name == "Final")
        self.assertEqual((final.scope, final.requirements), ("named", "reqs.md"))

    def test_governing_deadline_skips_a_named_only_one(self) -> None:
        self.assertEqual(estimate.governing_deadline(self.cfg, SUNDAY).name, "end of day")
        self.assertEqual(estimate.governing_deadline(self.cfg, SUNDAY).at, datetime(2026, 9, 20, 23, 59, tzinfo=CT))
        self.assertEqual(estimate.governing_deadline(self.governs, SUNDAY).name, "PCCAT 1st attempt")
        before = datetime(2026, 9, 20, 11, 0, tzinfo=CT)
        self.assertEqual(estimate.governing_deadline(self.cfg, before).name, "Final")
        self.assertEqual(rb.nearest_deadline(self.cfg, SUNDAY).name, "PCCAT 1st attempt")

    def test_the_horizon_and_the_estimates_stop_at_end_of_day(self) -> None:
        self.assertEqual(rb.horizon(self.cfg, SUNDAY), (datetime(2026, 9, 21, 0, 0, tzinfo=CT), "end of day"))
        est = rb.estimates(self.tasks, self.cfg, SUNDAY)
        self.assertEqual({e.of for e in est.values()}, {"end of day"})
        self.assertEqual(est["Leftover one"].label, "est. 1/2 → end of day")
        self.assertEqual({e.of for e in rb.estimates(self.tasks, self.governs, SUNDAY).values()}, {"PCCAT 1st attempt"})

    def test_an_unowned_task_ends_today_not_at_the_exam(self) -> None:
        b = rb.day_bar(self.by["Nobody's"], self.cfg, SUNDAY)
        self.assertEqual((b.end, b.end_src, b.label), (datetime(2026, 9, 20, 23, 59, tzinfo=CT), "deadline", "no estimate"))
        self.assertEqual(rb.day_bar(self.by["Nobody's"], self.governs, SUNDAY).end, datetime(2026, 9, 26, 23, 59, tzinfo=CT))

    def test_a_task_that_names_the_exam_still_draws_to_it(self) -> None:
        b = rb.day_bar(self.by["Study"], self.cfg, SUNDAY)
        self.assertEqual((b.end, b.end_src), (datetime(2026, 9, 26, 23, 59, tzinfo=CT), "due"))

    def test_the_countdown_keeps_the_exam(self) -> None:
        self.assertIn('data-deadline-name="PCCAT 1st attempt"', rb.render(SUNDAY_TRACKER, self.cfg, SUNDAY))

    def test_the_cli_names_the_horizon(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(CLAUDE_MD_EXAM)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-16-tracker.md").write_text(QUEUE_TRACKER)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rb.main(["--root", str(root), "--date", "2026-09-16"])
        # main() renders at the real clock, so the name is whichever governing deadline is next — never the exam.
        m = re.search(r"horizon=(Launch|Final|end of day) ", buf.getvalue())
        self.assertIsNotNone(m, buf.getvalue())


class DecisionDeadlineTest(unittest.TestCase):
    """A recorded deadline reaches the clock, markers and task horizon on the board."""

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())

    @staticmethod
    def tracker(*items: str | tuple[str, str]) -> str:
        rows = ""
        for item in items:
            when, name = item if isinstance(item, tuple) else ("12:00", item)
            rows += f'| {when} | {name} | "approved" |\n'
        return TRACKER.replace('| 11:15 | Draft release notes | "done" |\n',
                               '| 11:15 | Draft release notes | "done" |\n' + rows)

    def test_same_day_decision_reaches_clock_marker_and_horizon(self) -> None:
        tracker = self.tracker("deadline: Review cutoff 16:00")
        html = rb.render(tracker, self.cfg, NOW)
        self.assertIn('data-deadline="2026-09-16T16:00:00-05:00"', html)
        self.assertIn('data-deadline-name="Review cutoff"', html)

    def test_explicit_date_and_time_override_a_configured_deadline(self) -> None:
        tracker = self.tracker(("13:00", "deadline: Final 2026-09-16 17:00"),
                               ("12:00", "deadline: Final 2026-09-16 18:00"))
        cfg = rb.with_decision_deadlines(self.cfg, tracker, NOW.date())
        final = [d for d in cfg.deadlines if d.name == "Final"]
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0].at, datetime(2026, 9, 16, 17, 0, tzinfo=CT))
        self.assertIn('data-deadline="2026-09-16T17:00:00-05:00"',
                      rb.render(tracker, self.cfg, NOW))

    def test_changed_time_keeps_the_configured_requirements_file(self) -> None:
        cfg = parse_coordinator(CLAUDE_MD_REQ, today=NOW.date())
        updated = rb.with_decision_deadlines(cfg, self.tracker("deadline: Final 2026-09-16 17:00"),
                                             NOW.date())
        final = next(d for d in updated.deadlines if d.name == "Final")
        self.assertEqual(final.requirements, "daily/final-reqs.md")

    def test_time_only_uses_selected_tracker_day_not_render_instant(self) -> None:
        cfg = rb.with_decision_deadlines(self.cfg, self.tracker("deadline: Review cutoff 16:00"),
                                         NOW.date())
        cutoff = next(d for d in cfg.deadlines if d.name == "Review cutoff")
        self.assertEqual(cutoff.at.date(), NOW.date())

    def test_no_time_given_does_not_invent_an_instant(self) -> None:
        tracker = self.tracker("deadline: Exam, Sat 2026-09-26 (no time given)")
        cfg = rb.with_decision_deadlines(self.cfg, tracker, NOW.date())
        self.assertNotIn("Exam", {d.name for d in cfg.deadlines})
        self.assertNotIn('data-deadline-name="Exam"', rb.render(tracker, self.cfg, NOW))

    def test_named_only_decision_does_not_become_the_default_task_horizon(self) -> None:
        tracker = self.tracker("deadline: Exam 2026-09-17 09:00; named tasks only")
        cfg = rb.with_decision_deadlines(self.cfg, tracker, NOW.date())
        exam = next(d for d in cfg.deadlines if d.name == "Exam")
        self.assertEqual(exam.scope, "named")
        self.assertEqual(estimate.governing_deadline(cfg, NOW).name, "Launch")

    def test_future_decision_carries_into_the_next_daily_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "daily").mkdir()
            (root / "daily/2026-09-16-tracker.md").write_text(
                self.tracker("deadline: Review cutoff 2026-09-17 16:00"))
            today = TRACKER.replace("# Tracker 2026-09-16", "# Tracker 2026-09-17")
            cfg = rb.with_workspace_decision_deadlines(root, self.cfg, today, NOW.date() + timedelta(days=1))
        cutoff = next(d for d in cfg.deadlines if d.name == "Review cutoff")
        self.assertEqual(cutoff.at, datetime(2026, 9, 17, 16, 0, tzinfo=CT))

    def test_expired_decision_is_not_carried_forward(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "daily").mkdir()
            (root / "daily/2026-09-16-tracker.md").write_text(
                self.tracker("deadline: Review cutoff 16:00"))
            cfg = rb.with_workspace_decision_deadlines(root, self.cfg, TRACKER,
                                                        NOW.date() + timedelta(days=1))
        self.assertNotIn("Review cutoff", {d.name for d in cfg.deadlines})

    def test_cli_renders_a_carried_deadline_and_reports_its_horizon(self) -> None:
        today = datetime.now(CT).date()
        tomorrow = today + timedelta(days=1)
        yesterday = today - timedelta(days=1)
        deadline = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 16, tzinfo=CT)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "daily").mkdir()
            (root / "CLAUDE.md").write_text(CLAUDE_MD)
            (root / "daily" / f"{yesterday}-tracker.md").write_text(
                self.tracker(f"deadline: Review cutoff {tomorrow} 16:00"))
            (root / "daily" / f"{today}-tracker.md").write_text(TRACKER)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                page = rb.main(["--root", str(root), "--date", str(today)])
            html = page.read_text()
        self.assertIn(f'data-deadline="{deadline.isoformat()}"', html)
        self.assertIn('data-deadline-name="Review cutoff"', html)
        self.assertIn("horizon=Review cutoff", output.getvalue())

    def test_agent_rules_name_the_precise_decision_format(self) -> None:
        rules = rules_text()
        self.assertIn("deadline: <name> YYYY-MM-DD HH:MM", rules)
        self.assertIn("deadline: <name> HH:MM", rules)

    def test_agent_rules_say_a_workflow_step_and_a_state_change_take_no_issue(self) -> None:
        # #362, the user's rule: "you are NOT to file new issues for things that are part of the workflow (e.g. reviewing a mr)"
        # and "my rule about not creating issues to track moving issues between states". The Tasks `issue` sentences must carry,
        # each exactly (the audit and the dispatch check read the token `workflow`):
        #   "A workflow step \u2014 a review, or anything the pipeline itself performs \u2014 takes no issue: write `workflow` in its `issue` cell."
        #   "A change of state \u2014 a kanban stage, a hold label, a review or merge step \u2014 is recorded on the issue it concerns, in the stage column and the Log, never as a new issue."
        # "A standing session's placeholder is not a task and takes no issue" stays.
        rules = rules_text()
        for required in (
            "A workflow step \u2014 a review, or anything the pipeline itself performs \u2014 takes no issue: write `workflow` in its `issue` cell.",
            "A change of state \u2014 a kanban stage, a hold label, a review or merge step \u2014 is recorded on the issue it concerns, in the stage column and the Log, never as a new issue.",
            "A standing session's placeholder is not a task and takes no issue",
        ):
            with self.subTest(required=required):
                self.assertTrue(required in rules, f"the agent file does not say {required!r}")

    def test_agent_rules_carry_the_workflow_exception_wherever_they_ask_for_an_issue(self) -> None:
        # #366 review R10, R11, R12: three sentences still ask for an issue, or a sync of one, with no exception for a workflow row.
        # The agent file must say, each in the line that holds the anchor named here:
        #   Dispatch (anchor "file its issue where the block names a `Backlog:`"):
        #     "unless it is a workflow step, which takes the token `workflow`"
        #   Open the day, carry-over (anchor "4. Carry over:"), replacing "every carried task with no issue gets one filed now":
        #     "every carried task with no issue, other than a workflow step, gets one filed now"
        #   Pipeline kanban sync (anchor "sync its one issue with"):
        #     "a `workflow` row has no issue and takes no sync"
        #   Tasks bullet, one sentence, whole:
        #     "A review you are asked to do is a workflow step with its own `workflow` row; a review stage on a task is a change of state on that task's own issue."
        rules = rules_text()
        for anchor, required in (
            ("file its issue where the block names a `Backlog:`", "unless it is a workflow step, which takes the token `workflow`"),
            ("4. Carry over:", "every carried task with no issue, other than a workflow step, gets one filed now"),
            ("sync its one issue with", "a `workflow` row has no issue and takes no sync"),
        ):
            with self.subTest(anchor=anchor):
                lines = [line for line in rules.splitlines() if anchor in line]
                self.assertEqual(len(lines), 1, f"the agent file has no single line holding {anchor!r}")
                self.assertIn(required, lines[0], f"the line holding {anchor!r} does not say {required!r}")
        required = ("A review you are asked to do is a workflow step with its own `workflow` row; "
                    "a review stage on a task is a change of state on that task's own issue.")
        self.assertIn(required, rules, f"the agent file does not say {required!r}")

    def test_agent_rules_do_not_describe_the_board_views_that_were_removed(self) -> None:
        # #259 removed the Sessions tree, the day bar, the Tasks table and the `est.` label from the board. The agent file
        # still carries these sentences (Tasks bullet and `## Board`), each describing a view the board no longer draws:
        stale = (
            "The board draws `## Sessions` as a tree",                       # a Sessions tree section
            "the only thing the board draws on a bar and in a table",        # the day bar and the table
            "labels it `est.`",                                              # the `est.` label, Tasks bullet
            "labelled `est. M ~1h20m`",                                      # the `est.` label, Board section
            "`est. i/N → <deadline>`",                                       # the `est.` label before any task has closed
            'open to the deadline labelled "no estimate"',                   # the bar drawn open to the deadline
        )
        rules = rules_text()
        for phrase in stale:
            with self.subTest(phrase=phrase):
                self.assertFalse(phrase in rules, f"the agent file still says {phrase!r}")

    def test_agent_rules_say_a_blank_due_means_a_derived_forecast(self) -> None:
        # With `due` blank and an owner, the board derives an end (render_board.py, estimate.py) and the Flow chart draws the
        # remaining stages as forecast bars; only an unowned task gets none. The Tasks bullet must carry this sentence:
        #   "a blank `due` leaves the board to derive an end from the owner's queue and draw the remaining stages as forecast"
        # The clauses "it stays blank until a person fills it" and "you never fill it in to make the board look complete" stay.
        rules = rules_text()
        self.assertFalse("blank means no estimate" in rules, "the agent file still says a blank `due` means no estimate")
        required = "a blank `due` leaves the board to derive an end from the owner's queue and draw the remaining stages as forecast"
        self.assertTrue(required in rules, f"the agent file does not say {required!r}")
        for kept in ("it stays blank until a person fills it", "you never fill it in to make the board look complete"):
            with self.subTest(kept=kept):
                self.assertTrue(kept in rules, f"the agent file lost {kept!r}")

    def test_agent_rules_say_a_task_with_no_lane_or_stage_has_no_card_in_build(self) -> None:
        # #350: BUILD draws a card only for a task with a lane and a stage, so the agent file must answer "why is X not on the
        # board" for a task with no tree. The Tasks `lane`/`stage` sentence must read exactly:
        #   "A task with no tree (a decision, a relay, a console action) leaves both cells blank and draws no card in BUILD."
        # (the header, the tiles and the Flow charts still count it; the sentence need not say so).
        rules = rules_text()
        required = "leaves both cells blank and draws no card in BUILD"
        self.assertTrue(required in rules, f"the agent file does not say {required!r}")

    def test_agent_rules_say_a_carried_row_keeps_its_lane_and_stage(self) -> None:
        # #350: board-on-open-the-day's graders pin `build | implement` on the carried rows, and a carried task with no lane
        # and stage has no card in BUILD. So the Open the day carry-over sentence must name both. It must read exactly:
        #   "becomes a row in today's tracker Tasks, same item, owner, lane, stage, and checklist reference, with today's date as `since`"
        rules = rules_text()
        required = "same item, owner, lane, stage, and checklist reference"
        self.assertTrue(required in rules, f"the agent file does not say {required!r}")

    def test_agent_rules_do_not_say_the_board_draws_an_arrow_to_the_waiting_on_who(self) -> None:
        # Sessions are gone (#259), so the board draws no arrow. The `waiting on` bullet keeps its order (who first, then why)
        # and loses the clause "The who is what the board draws an arrow to".
        rules = rules_text()
        self.assertFalse("The who is what the board draws an arrow to" in rules, "the agent file still says the board draws an arrow to the who")
        kept = "`waiting on` names who first, then why"
        self.assertTrue(kept in rules, f"the agent file lost {kept!r}")


# --- the tracer bullet ----------------------------------------------------------------------------
# One task threaded through every layer: written with a `name`, parsed, and labelled by that name
# (the 24h axis and the swimlanes it was once placed on are gone, #259).

TRACKER_NAMED = """# Tracker 2026-09-16

Coordinator: coordinator. Board: board-7.

## Tasks

| name | item | owner | state | since | due | size | checklist |
|---|---|---|---|---|---|---|---|
| Cut the release branch | **Cut the release branch.** 23:40: handed to impl-2 with the checklist; 00:12: branch cut, suite green on the branch | impl-2 | running 10:30 | 10:30 | 23:00 | M | Checklist: cut |
|  | Write eval README | Robin | open | 09:00 | 17:00 |  | Checklist: readme |

## Sessions

## Log

- 09:00 opened the day
"""


class TracerTest(unittest.TestCase):
    """The one task, end to end. Every assertion here is a layer of the design."""

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.tracker = rb.parse_tracker(TRACKER_NAMED)

    def test_the_name_column_parses_and_an_unnamed_row_falls_back(self) -> None:
        named, unnamed = self.tracker.tasks
        self.assertEqual(named.name, "Cut the release branch")
        self.assertEqual(named.label, "Cut the release branch")
        self.assertEqual(named.owner, "impl-2")
        self.assertEqual(named.size, "M")
        self.assertEqual(named.warning, "")
        # No name written: the label is what short_name() has always produced.
        self.assertEqual(unnamed.name, "")
        self.assertEqual(unnamed.label, tr.short_name(unnamed.item))

    def test_the_six_and_seven_column_trackers_still_parse(self) -> None:
        """100 live rows are not rewritten in one go: all three header widths keep working."""
        old = rb.parse_tracker(TRACKER)
        self.assertEqual(old.tasks[0].label, "Write eval README")
        self.assertEqual(old.tasks[0].name, "")
        sized = rb.parse_tracker(TRACKER.replace(
            "| item | owner | state | since | due | checklist |",
            "| item | owner | state | since | due | size | checklist |"))
        self.assertEqual(sized.tasks[0].name, "")


# --- stage 0: render/model layer -----------------------------------------------------------------
# The render layer that lands the tracer bullet: section order (the Flow design's sections only, #259),
# task labels = task.label only. The 24h axis, the swimlanes and the DUE NEXT / BLOCKED sections are removed.

TRACER_TRACKER = """# Tracker 2026-09-16

Coordinator: coordinator. Board: board-7.

## Tasks

| name | item | owner | state | since | due | size | checklist |
|---|---|---|---|---|---|---|---|
| Cut the release branch | **Cut the release branch.** 23:40: handed to impl-2 with the checklist; 00:12: branch cut, suite green on the branch | impl-2 | running 10:30 | 10:30 | Launch | M | Checklist: cut |
|  | Write eval README | Robin | open | 09:00 | 17:00 |  | Checklist: readme |
| Security audit | Security audit: static analysis pass | subagent | open | 09:00 |  | S | Checklist: audit |
| Old task | Old task: did it yesterday | Robin | done 11:15–12:00 | 2026-09-15 |  | M | Checklist: old |

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| a1b2c3 | impl-2 | working | the release branch | | 15:00 | TDD | none | 14:20 |

## Log

- 09:00 opened the day
"""


class SectionOrderTest(unittest.TestCase):
    """The page's sections appear in the Flow artboard's order (#259).

    Tabs, header, tiles, BUILD, the MERGE ORDER / DECISIONS / WORKERS panels, then the Flow charts (this tracker
    has no stage history, so none; test_board_flow_design.py pins their place after the panels).
    Due next, Blocked, the 24-hour day strip, Lanes and Sessions are not in the design, so none is drawn.
    """

    ORDER = ['<nav class="tabs"', '<header class="panels-head"', '<nav class="panels-tiles"', "<h2>Build</h2>",
             '<div class="panels">']

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACER_TRACKER, self.cfg, NOW)

    def test_section_headings_appear_in_the_right_order(self) -> None:
        at = [self.html.index(mark) for mark in self.ORDER]
        self.assertEqual(at, sorted(at))

    def test_the_headings_and_section_ids_are_exactly_the_designs(self) -> None:
        # An allow-list, not a list of the removed names: any renamed or extra <h2>, id or section fails.
        # This tracker has no stage history, so the two Flow charts are not drawn (test_board_flow_design.py pins them).
        h2, ids, sections = outline(self.html)
        self.assertEqual(h2, ["Build"])
        self.assertEqual(ids, ["flow", "merge", "decisions", "workers"])
        self.assertEqual(sections, 4)


class InlineMarksTest(unittest.TestCase):
    """The coordinator writes `**bold**` headlines and `code` into items, facts and Resume values; the board showed the punctuation.

    Finding 98: 50 of 100 live task names began with literal asterisks. A nowrap name drops the marks and
    keeps the words; prose that wraps — history bullets, session facts, Resume values — renders them. The
    citation attributes (`data-item`) stay raw, because that is what the graders match.
    """

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())

    def test_unmark_drops_pairs_and_keeps_a_lone_marker(self) -> None:
        self.assertEqual(md.unmark("**Deploy main.** x"), "Deploy main. x")
        self.assertEqual(md.unmark("a ** b"), "a ** b")
        self.assertEqual(md.unmark("**a** and **b**"), "a and b")


class MarkSpansTest(unittest.TestCase):
    """A mark pair is a span like a parenthesis: a clause split through the middle of one leaves half a mark on each side.

    Found on the live board after the first fix: 21 history bullets held an orphan `**`, all of them a
    bold clause containing a full stop, which `_depth0_split` cut in two. A long Resume field kept its
    marks in the summary it folds behind, and a backtick in a name is the same noise as an asterisk.
    """

    def test_a_split_never_falls_inside_a_bold_span(self) -> None:
        text = "Head. **22:19: copied into bruno/ (source untouched). Zach wants this:** launch.php reads it. Tail"
        self.assertEqual(
            tr._depth0_split(text),
            ["Head", "**22:19: copied into bruno/ (source untouched). Zach wants this:** launch.php reads it", "Tail"],
        )

    def test_an_unbalanced_item_splits_as_it_always_did(self) -> None:
        self.assertEqual(tr._depth0_split("**half open. and then"), ["**half open", "and then"])


    def test_a_name_drops_backticks_and_keeps_a_lone_star(self) -> None:
        self.assertEqual(md.unmark("D8 `AGENT_FRAME_ANCESTORS` defaults to `*`"), "D8 AGENT_FRAME_ANCESTORS defaults to *")
        self.assertEqual(md.unmark("agent `39e516f`, 30 behind"), "agent 39e516f, 30 behind")


PIPED_TRACKER = SIZED_TRACKER.replace(
    "| A oldest | worker | open | 2026-09-15 |  | M | c |",
    "| A oldest | worker | open | 2026-09-15 |  | M | c |\n"
    "| P coded | worker | done 00:28–01:36 | 2026-09-19 | Final | L | c |\n"
    "| P bare | worker | running 09:00 | 2026-09-16 |  | S | c |",
).replace("| P coded |", '| P coded, `"ok"|"degraded"` in prose |').replace("| P bare |", "| P bare A|B plain |")


class CellSpanTest(unittest.TestCase):
    """A cell is a span too: the delimiter is a pipe nobody spoke for."""

    def test_a_pipe_inside_a_backtick_span_is_not_a_delimiter(self) -> None:
        self.assertEqual(md.cells('| a | `x|y` | b |'), ["a", "`x|y`", "b"])

    def test_an_escaped_pipe_is_a_pipe_and_loses_its_backslash(self) -> None:
        self.assertEqual(md.cells(r"| a | x \| y | b |"), ["a", "x | y", "b"])

    def test_a_cell_holding_both_comes_back_once(self) -> None:
        self.assertEqual(md.cells(r"| a | `a|b` \| c | d |"), ["a", "`a|b` | c", "d"])

    def test_an_odd_backtick_splits_as_it_always_did(self) -> None:
        """A typo degrades to the old behaviour instead of swallowing the rest of the row."""
        self.assertEqual(md.cells("| a | `x|y | b |"), ["a", "`x", "y", "b"])

    def test_the_ordinary_rows_parse_exactly_as_before(self) -> None:
        self.assertEqual(md.cells("| Write eval README | Robin | open | 09:00 | 17:00 | c |"),
                         ["Write eval README", "Robin", "open", "09:00", "17:00", "c"])
        self.assertEqual(md.cells("|---|---|---|"), ["---", "---", "---"])

    def test_a_session_row_keeps_its_columns_when_a_cell_carries_a_pipe(self) -> None:
        cells = md.cells('| ab12cd | impl-2 | working | reads `"ok"|"degraded"` | | | | | 09:00 |')
        self.assertEqual(len(cells), 9)
        self.assertEqual(cells[3], 'reads `"ok"|"degraded"`')


class TaskAnchorTest(unittest.TestCase):
    """An over-wide row is re-read from the one cell a machine can recognise: its state."""

    def setUp(self) -> None:
        self.by = {task.item: task for task in rb.parse_tracker(PIPED_TRACKER).tasks}

    def test_a_backticked_pipe_needs_no_recovery_at_all(self) -> None:
        task = self.by['P coded, `"ok"|"degraded"` in prose']
        self.assertEqual(task.warning, "")
        self.assertEqual((task.owner, task.state, task.due, task.size, task.checklist),
                         ("worker", "done 00:28–01:36", "Final", "L", "c"))

    def test_a_bare_pipe_in_the_item_is_anchored_back_onto_its_columns(self) -> None:
        task = self.by["P bare A|B plain"]
        self.assertEqual((task.owner, task.state, task.since, task.size, task.checklist),
                         ("worker", "running 09:00", "2026-09-16", "S", "c"))

    def test_a_recovered_row_still_warns(self) -> None:
        self.assertIn("8 cells", self.by["P bare A|B plain"].warning)

    def test_surplus_on_the_right_lands_in_the_checklist(self) -> None:
        t = rb.parse_tracker(TRACKER)
        long = [task for task in t.tasks if task.item == "Long"][0]
        self.assertEqual((long.owner, long.state, long.since, long.due), ("Robin", "open", "09:00", "10:00"))
        self.assertEqual(long.checklist, "x | extra | cells")
        self.assertIn("8 cells", long.warning)

    def test_two_state_cells_fall_back_to_the_left_anchored_parse(self) -> None:
        """Two candidates is not an anchor. The row keeps the parse it was written with, and the warning speaks."""
        text = SIZED_TRACKER.replace("| A oldest | worker | open | 2026-09-15 |  | M | c |",
                                     "| A|open|B | worker | open | 2026-09-15 |  | M | c |")
        task = [l for l in rb.parse_tracker(text).tasks if l.item == "A"][0]
        self.assertEqual((task.owner, task.state), ("open", "B"))
        self.assertIn("9 cells", task.warning)

    def test_no_state_cell_falls_back_to_the_left_anchored_parse(self) -> None:
        text = SIZED_TRACKER.replace("| A oldest | worker | open | 2026-09-15 |  | M | c |",
                                     "| A|B | worker | dancing | 2026-09-15 |  | M | c |")
        task = [l for l in rb.parse_tracker(text).tasks if l.item == "A"][0]
        self.assertEqual(task.owner, "B")
        self.assertIn("8 cells", task.warning)

    def test_a_short_row_is_never_anchored(self) -> None:
        text = SIZED_TRACKER.replace("| C newer | worker | open | 2026-09-16 |  |  | c |", "| C newer | worker | open |")
        task = {l.item: l for l in rb.parse_tracker(text).tasks}["C newer"]
        self.assertEqual(task.since, "")
        self.assertIn("3 cells", task.warning)


class TaskStateCarriesAShaTest(unittest.TestCase):
    """`done HH:MM–HH:MM <sha>` — the commit that carried the task's change onto main.

    chief-of-stuff-improvements 0.13.0 (`3c8476e`) makes `landed()` three-valued, and only a
    `merge-base --is-ancestor` that answers yes clears a task. That needs the sha written in the
    state cell. `TASK_STATE` is the vocabulary as a WHOLE cell, so a sha-bearing state stops being
    recognisable — and its one caller is `_anchor`, the recovery for a row that grew a stray pipe.
    A task that carries evidence would be the one task that cannot be recovered.
    """

    SHA = "db4aa3b"

    def test_the_vocabulary_admits_a_trailing_sha(self) -> None:
        for cell in (f"done 21:16–22:05 {self.SHA}", f"done 21:16 {self.SHA}", f"done {self.SHA}"):
            self.assertTrue(tr.TASK_STATE.match(cell), cell)

    def test_it_still_admits_every_state_without_one(self) -> None:
        for cell in ("open", "waiting", "orphaned", "done", "running 09:00",
                     "done 21:16", "done 21:16–22:05"):
            self.assertTrue(tr.TASK_STATE.match(cell), cell)

    def test_it_does_not_admit_prose_after_the_state(self) -> None:
        """The cell is recognisable on sight or it is not an anchor. `done, merged by impl-2` is
        prose, and admitting it would let an item cell claim the anchor."""
        for cell in ("done, merged by impl-2", "done 21:16 by hand", "running 09:00 slowly",
                     "done zzzz", "done 21:16–22:05 not-a-sha!"):
            self.assertIsNone(tr.TASK_STATE.match(cell), cell)

    def test_a_sha_bearing_row_with_a_stray_pipe_still_recovers(self) -> None:
        text = SIZED_TRACKER.replace("| A oldest | worker | open | 2026-09-15 |  | M | c |",
                                     f"| A|B plain | worker | done 09:00–10:00 {self.SHA} | 2026-09-15 |  | M | c |")
        task = {l.item: l for l in rb.parse_tracker(text).tasks}["A|B plain"]
        self.assertEqual(task.owner, "worker")
        self.assertEqual(task.state, f"done 09:00–10:00 {self.SHA}")
        self.assertEqual(task.size, "M")

    def test_the_sha_reaches_the_board_as_written(self) -> None:
        """Confirmed against the rendered artifact, not the call graph: chief-of-stuff-improvements
        read the code and said the sha would show; it also said its own read was a hypothesis."""
        text = ("# T\n\n## Tasks\n\n" + LANED_HEAD
                + f"| A oldest | A oldest | worker | done 09:00–10:00 {self.SHA} | 2026-09-15 |  | M | build | merge |  | c |\n")
        cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        html = rb.render(text, cfg, NOW)
        self.assertIn(self.SHA, html)


class StateDisputesProseTest(unittest.TestCase):
    """3a: a task whose state cell and whose own prose disagree about whether it is finished.

    Found because four of eleven live tasks carried a stale state at 03:56 (two overstating
    progress, two understating it) and no reader could see any of them: `TASK_STATE` is used
    only as a stray-pipe anchor, never as validation, so a task's state is checked by nothing.
    A freshness check needs to know about the world. This one does not — the row already
    carries its own contradiction, and comparing the state cell against `item` is pure text
    available at render time.

    The vocabulary is `migrate_backlog.DONE_WORDS`, matched case-sensitively on purpose: a
    shouted DONE is a claim about this row, `resolved by the release` is an ordinary sentence.
    Case-insensitive matching flagged 17 rows in the migration report, mostly the latter.
    """

    TASKS = (
        "## Tasks\n\n"
        "| item | owner | state | since | due | checklist |\n"
        "|---|---|---|---|---|---|\n"
    )

    def task(self, item: str, state: str) -> "rb.Task":
        text = f"# T\nBoard: b\n\n{self.TASKS}| {item} | Robin | {state} | 09:00 |  | c |\n"
        return rb.parse_tracker(text).tasks[0]

    def test_an_active_task_whose_item_shouts_done_is_flagged(self) -> None:
        w = self.task("Rules engine split — LANDED on main", "open").warning
        self.assertIn("open", w)
        self.assertIn("LANDED", w)

    def test_a_closed_task_saying_the_same_thing_agrees_with_itself(self) -> None:
        """`done` plus a shouted DONE is a row in agreement, not a dispute."""
        self.assertEqual(self.task("Rules engine split — LANDED", "done 11:15").warning, "")

    def test_ordinary_prose_is_not_a_claim_about_this_row(self) -> None:
        """The whole reason the match is case-sensitive."""
        self.assertEqual(self.task("Blocked until the loader bug is resolved", "open").warning, "")

    def test_every_active_state_is_checked_not_only_open(self) -> None:
        """Two of the four stale cells said `waiting` and one said `orphaned`."""
        for state in ("waiting", "orphaned", "running 10:30"):
            with self.subTest(state=state):
                self.assertIn("DONE", self.task("Ship it — DONE", state).warning)

    def test_a_task_that_does_not_dispute_itself_is_untouched(self) -> None:
        self.assertEqual(self.task("Write eval README", "open").warning, "")

    def test_the_dispute_reaches_the_printed_warning_count(self) -> None:
        """A warning nothing counts is telemetry, not a warning (`:1914`)."""
        text = (
            f"# T\nBoard: b\n\n{self.TASKS}"
            "| Ship it — DONE | Robin | open | 09:00 |  | c |\n"
            "| Write eval README | Robin | open | 09:00 |  | c |\n"
        )
        tasks = rb.parse_tracker(text).tasks
        self.assertEqual(sum(1 for task in tasks if task.warning), 1)

    def test_the_vocabulary_does_not_drift_from_the_migration_report(self) -> None:
        """Two lists of done-words would diverge silently; this is the only thing watching."""
        import migrate_backlog as mb
        self.assertEqual(tr.DONE_WORDS.pattern, mb.DONE_WORDS.pattern)


class StandingRowsAreNotTasksTest(unittest.TestCase):
    """Zach, 2026-09-22 19:28: "standing tasks with NO TASKS should not show up in the task list. A task can
    either be unassigned or in a task. we don't finish TASKS, we finish TASKS". A standing session's row
    exists only because spawn_session.py cannot start a session without one; it is not work."""

    TRACKER = """# Tracker 2026-09-16

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| impl02 — standing implementer | impl02: standing implementer. Register with the coordinator, then wait idle for an assignment; write nothing until one arrives. | impl02 | running 10:00 | 2026-09-16 |  | S | build | implement |  |  |
| Fix the parser | Fix the parser: make it strict | impl02 | open | 11:00 |  | M | build | implement |  |  |

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| a1b2c3 | impl02 | working | the parser | | | | none | 14:20 |
"""

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(self.TRACKER, self.cfg, NOW)

    @staticmethod
    def row(name: str, item: str, owner: str, state: str = "running 10:00") -> "rb.Task":
        return rb.Task(item, owner, state, "2026-09-16", "", "", name=name)

    def test_a_standing_row_is_recognised_by_its_item_or_its_name(self) -> None:
        self.assertTrue(self.row("", "impl02: standing implementer. Register with the coordinator.", "impl02").standing)
        self.assertTrue(self.row("reviewer01 — standing reviewer", "Wait for a review.", "reviewer01").standing)
        self.assertTrue(self.row("docs01 - standing docs", "Wait.", "docs01").standing)
        self.assertTrue(self.row("", "**impl03**: standing implementer.", "impl03 [f7b55f]").standing, "the owner's ref is not part of its name")

    def test_a_real_task_or_another_owners_name_is_not_standing(self) -> None:
        self.assertFalse(self.row("Review lab trend", "Review lab trend: review feat/lab-trend", "reviewer01").standing)
        self.assertFalse(self.row("impl02 — standing implementer", "impl02: standing implementer.", "impl03").standing)
        self.assertFalse(self.row("Standing desk", "Order a standing desk", "Robin").standing)

    def test_the_standing_row_is_on_no_task_surface(self) -> None:
        body = self.html.split("</style>", 1)[1]
        self.assertNotIn("standing implementer", body)
        self.assertIn("Fix the parser", body)
        self.assertIn(" · 1 task</div>", body)  # the header ends at the count now that " · N in no lane" is gone (#350)


# Zach, 2026-09-22 22:20: "each entry in the task tracker is actually backed by an entry in github".
CLAUDE_MD_ISSUES = CLAUDE_MD.replace(
    "- Human-only actions: spending money",
    "- Backlog: GitHub issues; repo https://github.com/o/backlog (private)\n- Human-only actions: spending money")

TRACKER_ISSUED = """# Tracker 2026-09-16

Coordinator: coordinator. Board: board-7.

## Tasks

| name | item | owner | state | since | due | size | issue | checklist |
|---|---|---|---|---|---|---|---|---|
| Cut the release | Cut the release branch | impl-2 | running 10:30 | 10:30 | 23:00 | M | #9 | Checklist: cut |
| Write README | Write eval README | Robin | open | 09:00 | 17:00 | S |  | Checklist: readme |
| Elsewhere | Fix the other repo | Robin | open | 09:00 | 17:00 | S | https://github.com/x/y/issues/4 | Checklist: other |
| Garbled | Garbled issue cell | Robin | waiting | 09:00 | 17:00 | S | TBD | Checklist: g |
| Old done | Shipped before the rule | Robin | done 08:00–09:00 | 08:00 |  | S |  | Checklist: old |
| impl-3 — standing implementer | impl-3: standing implementer; wait idle | impl-3 | open | 09:00 |  |  |  |  |

## Sessions

## Log

- 09:00 opened the day
"""


class IssueColumnTest(unittest.TestCase):
    """The tracker's `issue` column, and the board's reading of it: a link, or a warning where a task has none."""

    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD_ISSUES, today=NOW.date())

    def test_nine_columns_parse(self) -> None:
        tasks = rb.parse_tracker(TRACKER_ISSUED).tasks
        self.assertEqual([l.issue for l in tasks[:4]], ["#9", "", "https://github.com/x/y/issues/4", "TBD"])
        self.assertEqual(tasks[0].checklist, "Checklist: cut")
        self.assertEqual(tasks[0].warning, "")

    def test_a_stray_pipe_is_anchored_in_a_nine_column_row(self) -> None:
        text = TRACKER_ISSUED.replace("| #9 | Checklist: cut |", "| #9 | Checklist: cut | then tag |", 1)
        task = rb.parse_tracker(text).tasks[0]
        self.assertEqual((task.item, task.state, task.issue, task.checklist), ("Cut the release branch", "running 10:30", "#9", "Checklist: cut | then tag"))

    def test_backlog_is_read_from_the_block(self) -> None:
        self.assertEqual(self.cfg.backlog, GitHubBacklog("o/backlog"))
        self.assertIsNone(parse_coordinator(CLAUDE_MD, today=NOW.date()).backlog)


    def test_a_gitlab_backlog_is_read_from_the_block(self) -> None:
        """Zach, 2026-09-23 22:40: "make everything use gitlab now that we have that going"."""
        text = CLAUDE_MD.replace("- Human-only", "- Backlog: GitLab; host https://gl.example; project o/backlog\n- Human-only")
        self.assertEqual(parse_coordinator(text, today=NOW.date()).backlog, Backlog(host="https://gl.example", project="o/backlog"))

    def test_an_unreadable_backlog_line_is_a_config_error(self) -> None:
        with self.assertRaises(rb.ConfigError):
            parse_coordinator(CLAUDE_MD.replace("- Human-only", "- Backlog: GitHub issues\n- Human-only"), today=NOW.date())


class FlowRefReviewTest(unittest.TestCase):
    """PR 461 R2: change refs and issue refs with the same number are different destinations."""

    CHANGE_URLS = (
        "https://forge.example/group/project/-/merge_requests/51",
        "https://github.com/group/project/pull/51",
    )

    def flow(self, issue: str, item: str) -> str:
        from test_flow_chart import LANES, NOW, TODAY, flow_launch_tracker

        tracker = flow_launch_tracker([("Build parser", item, issue)], [(item, "worker-1", "00:10")])
        cfg = parse_coordinator("## Coordinator\n\n- User: User\n- Daily log dir: `daily/`\n"
                                "- Tracker: `daily/<date>-tracker.md`\n- Timezone: America/Chicago\n", today=TODAY)
        html = rb.render(tracker, cfg, NOW, lanes=LANES, tracker_day=TODAY)
        self.assertIn('<span class="gantt-name">Build parser</span>', html)
        return html[html.index("<h2>Flow ·"):]

    def test_a_change_url_flow_ref_does_not_link_to_an_issue_page(self) -> None:
        for url in self.CHANGE_URLS:
            with self.subTest(url=url):
                flow = self.flow("workflow", f"Review {url} and check diagnostics")
                self.assertNotRegex(flow, r'href="/issues/\d+"')

    def test_merge_request_and_pull_flow_refs_are_labelled_as_changes(self) -> None:
        # Pin !51 for both merge_requests and GitHub pull URLs; #51 is reserved for issues.
        for url in self.CHANGE_URLS:
            with self.subTest(url=url):
                flow = self.flow("workflow", f"Review {url} and check diagnostics")
                self.assertEqual(set(re.findall(r'class="gantt-ref"[^>]*>([^<]*)<', flow)), {"!51"})

    def test_issue_key_and_issue_url_flow_refs_still_link_to_the_issue_page(self) -> None:
        for ref in ("group/project#7", "https://forge.example/group/project/-/issues/7",
                    "https://github.com/group/project/issues/7"):
            with self.subTest(ref=ref):
                flow = self.flow(ref, "Build the parser")
                self.assertEqual(set(re.findall(r'href="(/issues/[^"]*)"', flow)), {"/issues/7"})

    def test_forge_issue_url_flow_labels_show_only_the_issue_number(self) -> None:
        # #459: display is #N, independent of the full key used to merge passes.
        for url in ("https://forge.example/group/project/-/issues/7",
                    "https://github.com/group/project/issues/7"):
            with self.subTest(url=url):
                flow = self.flow(url, "Build the parser")
                self.assertEqual(re.findall(r'class="gantt-ref"[^>]*>([^<]*)<', flow), ["#7", "#7"])

    def test_forge_issue_url_flow_ref_titles_keep_the_full_url(self) -> None:
        # #459 decision: the short label's title retains the tracker cell's full URL.
        for url in ("https://forge.example/group/project/-/issues/7",
                    "https://github.com/group/project/issues/7"):
            with self.subTest(url=url):
                flow = self.flow(url, "Build the parser")
                refs = re.findall(r'<a\b([^>]*\bclass="gantt-ref"[^>]*)>', flow)
                self.assertEqual(len(refs), 2)
                for attrs in refs:
                    self.assertIn(f'title="{url}"', attrs)

    def test_bare_issue_flow_labels_and_task_page_links_stay_unchanged(self) -> None:
        flow = self.flow("#7", "Build the parser")
        refs = re.findall(r'<a\b([^>]*\bclass="gantt-ref"[^>]*)>([^<]*)</a>', flow)
        self.assertEqual(len(refs), 2)
        for attrs, label in refs:
            self.assertEqual(label, "#7")
            self.assertIn('href="/issues/7"', attrs)

    def test_markdown_issue_flow_ref_titles_escape_quotes_and_angle_brackets(self) -> None:
        # PR 469 R1: the issue cell's markdown label must not become HTML attributes.
        url = "https://forge.example/group/project/-/issues/7"
        issue = f'[x" onmouseover="alert(1)<b>]({url})'
        flow = self.flow(issue, "Build the parser")
        rows = re.findall(r'<div class="gantt-row">[^\n]+', flow)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn(f'title="[x&quot; onmouseover=&quot;alert(1)&lt;b&gt;]({url})"', row)
            # Checking the entire row covers every attribute, including malformed ones.
            self.assertNotIn('" onmouseover=', row)
            self.assertNotIn("<b>", row)


class SettingsLineTest(unittest.TestCase):
    """`Settings: <path>` names the workspace's chief-of-stuff.toml (Zach, 2026-09-22 22:20)."""

    def test_read(self) -> None:
        text = CLAUDE_MD.replace("- Human-only", "- Settings: `chief-of-stuff.toml`\n- Human-only")
        self.assertEqual(parse_coordinator(text, today=NOW.date()).settings_path, "chief-of-stuff.toml")

    def test_absent(self) -> None:
        self.assertIsNone(parse_coordinator(CLAUDE_MD, today=NOW.date()).settings_path)

    def test_prose_after_the_path_is_ignored(self) -> None:
        text = CLAUDE_MD.replace("- Human-only", "- Settings: chief-of-stuff.toml (notify, day times)\n- Human-only")
        self.assertEqual(parse_coordinator(text, today=NOW.date()).settings_path, "chief-of-stuff.toml")


class TasksHeadingTest(unittest.TestCase):
    """#33 (Zach, 2026-09-23): a row is a task, so the table is `## Tasks`; a tracker written before still says `## Lanes`."""

    def test_an_older_tracker_headed_lanes_reads_the_same(self) -> None:
        old = TRACKER.replace("## Tasks", "## Lanes")
        self.assertEqual(rb.parse_tracker(old).tasks, rb.parse_tracker(TRACKER).tasks)


TRACKER_LANED = """# Tracker 2026-09-16

Coordinator: coordinator. Board: board-7.

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Cut the release | Cut the release branch | impl-2 | running 10:30 | 10:30 | 23:00 | M | build | review | #9 | Checklist: cut |
| Write README | Write eval README | Robin | open | 09:00 | 17:00 | S | build | triage |  | Checklist: readme |
| Console | Rotate the key in the console | Robin | open | 09:00 | 17:00 | S |  |  |  | Checklist: key |

## Sessions

## Log

- 09:00 opened the day
"""


class LaneColumnsTest(unittest.TestCase):
    """#33: `lane` names a lane from the toml and `stage` is where the task is in it; a gate waits on the user."""

    LANES = {"build": st.Lane(("implement", "pr", "review", "triage", "merge"), ("triage", "merge"))}

    def test_eleven_columns_parse(self) -> None:
        tasks = rb.parse_tracker(TRACKER_LANED).tasks
        self.assertEqual([(t.lane, t.stage) for t in tasks[:3]], [("build", "review"), ("build", "triage"), ("", "")])
        self.assertEqual((tasks[0].issue, tasks[0].checklist, tasks[0].warning), ("#9", "Checklist: cut", ""))
        self.assertTrue(all(not t.warning for t in tasks), [t.warning for t in tasks])


    def test_the_board_draws_the_stage_columns_and_a_task_with_no_lane_is_in_none_of_them(self) -> None:
        # #350: NO LANE is gone from the board, so a task with no lane is drawn in no BUILD column and no strip.
        cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        html = rb.render(TRACKER_LANED, cfg, NOW, lanes=self.LANES)
        self.assertIn('<section id="flow"><h2>Build</h2>', html)
        build = html.split('<section id="flow">')[1].split('<div class="panels">')[0]
        self.assertIn("Cut the release", build)
        self.assertNotIn("Console", build)
        self.assertNotIn("columns-foot", html)
        self.assertNotRegex(html, r"(?i)no[ -]lane")
        # A tracker with no lanes has no stage columns, and its tasks are not drawn in BUILD at all.
        plain = rb.render(TRACKER, cfg, NOW)
        self.assertIn('<figure class="columns"', plain)
        self.assertNotIn("<table", plain.split('<section id="flow">')[1].split('<div class="panels">')[0])
        self.assertNotIn('class="columns-card', plain)  # the bare word is also in the page's CSS



    def test_the_build_line_counts_issues_not_workflow_rows(self) -> None:
        # #366 review R9: a `workflow` issue cell is no issue, so three open tasks, one with #9 and two with the token, are one issue.
        tracker = TRACKER_LANED.replace("| Robin | open | 09:00 | 17:00 | S | build | triage |  |", "| Robin | open | 09:00 | 17:00 | S | build | triage | workflow |"
                                        ).replace("| Robin | open | 09:00 | 17:00 | S |  |  |  |", "| Robin | open | 09:00 | 17:00 | S |  |  | workflow |")
        self.assertEqual([t.issue for t in rb.parse_tracker(tracker).tasks], ["#9", "workflow", "workflow"])
        cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        html = rb.render(tracker, cfg, NOW, lanes=self.LANES)
        self.assertIn('<div class="meta">1 issue · 0 merge requests</div>', html)
        self.assertNotRegex(html, r'href="[^"]*workflow')
        self.assertNotIn("#workflow", html)


class WorkflowTokenTest(unittest.TestCase):
    """#366 review R15: the token is tracker-row vocabulary, so `Task` answers it once and audit_tasks, dispatch_prompt,
    render_board's issue count and the launcher ask the row instead of comparing the raw string."""

    @staticmethod
    def row(issue: str) -> tr.Task:
        return tr.Task(item="Review it", owner="unassigned", state="open", since="09:00", due="", checklist="c", issue=issue)

    def test_workflow_is_true_only_for_the_exact_token_once_whitespace_is_stripped(self) -> None:
        for cell in ("workflow", " workflow ", "workflow\t"):
            self.assertIs(self.row(cell).workflow, True, repr(cell))
        for cell in ("", "Workflow", "WORKFLOW", "workflow.", "workflows", "my workflow", "#14", "o/backlog#14"):
            self.assertIs(self.row(cell).workflow, False, repr(cell))


class DensityTest(unittest.TestCase):
    """The page size guard from 0.3.1, restored: a board with the canonical fixture stays under 32 KB.

    This is the cap that the plan says is 'a real guard on this change': hover panels, lane tables,
    and any other markup must not blow the page. The 0.3.1 test measured `len(html)` against 16 KB
    on a smaller fixture; the Stage 0 fixture is larger, but 32 KB is the budget now.
    """

    def test_item_text_bounded(self) -> None:
        cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        html = rb.render(TRACER_TRACKER, cfg, NOW)
        self.assertLess(len(html), 32_000,
                        f"Board page is {len(html)} bytes, budget is 32 000")

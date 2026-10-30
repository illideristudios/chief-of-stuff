"""tracker_read.py (#46): `chief-of-stuff tracker` reads the tracker through the shared parser and writes nothing.

    chief-of-stuff tracker --root R [--date YYYY-MM-DD] header
    chief-of-stuff tracker --root R [--date YYYY-MM-DD] sections
    chief-of-stuff tracker --root R [--date YYYY-MM-DD] section <name>
    chief-of-stuff tracker --root R [--date YYYY-MM-DD] tasks [--not <state>]... [--state <state>]... [--issue <ref>]

The parser is the oracle: what a row means is `parse_tracker`'s to say, and these tests ask it rather than
restating it. The command is a presenter over it.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backlog  # noqa: E402
from _vendor.toon_format import decode as toon_decode  # noqa: E402
from md import section  # noqa: E402
from tracker import SHORT_NAME, Task, change_keys, clip_name, launch_row, launcher, parse_tracker  # noqa: E402
from workspace import read_config  # noqa: E402
import tracker_read  # noqa: E402

DAY = "2026-09-25"
HOST = "https://labs.example.test"
CLAUDE = ("# Workspace\n\n## Coordinator\n\n- User: Robin\n- Timezone: America/Chicago\n"
          "- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
          f"- Backlog: GitLab issues; host {HOST}; project team/app\n")
FIELDS = ["name", "owner", "state", "stage", "issue"]
# Each item cell is a paragraph carrying a word no other cell holds.
PROSE = ("It began as a one-line note and grew: the first attempt stalled on a missing fixture, the second landed "
         "half of it, and what is left is written here in full so that nobody has to ask again what was meant. ")
TRACKER = f"""# Tracker {DAY}

Coordinator: relay-12. Board: none.

## Resume

- As of: 09:40 — relay-12 [111111]
- In flight: nothing; the audit poll is out
- Next: read worker-a's reply
- Waiting on: Robin — the release notes owner
- Re-arm: the 30-minute check
- Verified 09:40: agent /ready 200 (09:38)

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Audit the upload endpoint | {PROSE}ITEMWORD1 closes it. | worker-a | running 09:30 | 09:00 |  | M | build | implement | #7 |  |
| Draft release notes | {PROSE}ITEMWORD2 closes it. | Robin | open | 09:00 |  | S | docs | plan | [#9]({HOST}/team/app/-/issues/9) |  |
|  | Rotate the staging credentials: {PROSE}ITEMWORD3 closes it. | unassigned |  | 09:05 |  |  |  |  |  |  |
| Migrate the nightly export job to the new queue and retire the old cron entry | {PROSE}ITEMWORD4 closes it. | worker-b | waiting | 09:10 |  | L | build | review | other/repo#7 |  |
| Report readiness | The endpoint reports `"ok"|"degraded"` and the flag reads a \\| b. {PROSE}ITEMWORD5 closes it. | worker-c | orphaned | 09:15 |  | M | build | fix | #11 |  |
| Ship the changelog | {PROSE}ITEMWORD6 closes it. | worker-a | done 08:10–08:55 | 08:00 |  | S | docs | main | #12 |  |
|  | Retire EARLYMARK the legacy importer once every caller has moved to the queue and the last nightly run that still depends on LATEMARK it has been watched through. {PROSE}ITEMWORD7 closes it. | unassigned | open | 09:20 |  | M |  |  |  |  |

## Decisions

| time | item | Robin's words |
|---|---|---|

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| a1b2c3 | worker-a | working | the upload audit | none | 11:00 | none | none | 09:30 |

## File ownership

- Audit the upload endpoint: worktree `wt-audit` · src/a/
  - tests under tests/a/ go with it
- Report readiness: worktree `wt-ready` · src/b/ `x|y`

## Log

- 09:00 opened the day
- 09:30 audit assigned to worker-a
"""
# Two rows the parser warns about. A running one with two unescaped `|` in its item: thirteen cells where the header
# has eleven, which the parser recovers into a label longer than the clip. A done one a cell short.
WARNED_ROWS = ("| Tune the limiter | The flag reads on | off in the config and yes | no in the notes, every time. | worker-d "
               "| running 10:00 | 09:20 |  | S | build | implement | #13 |  |\n"
               "| Archive the old runbook | Moved to the wiki. | Robin | done 08:30 | 08:00 |  | S |  |  |  |\n")
WARNED = TRACKER.replace("\n## Decisions", WARNED_ROWS + "\n## Decisions")
HEADINGS = ["Resume", "Tasks", "Decisions", "Sessions", "File ownership", "Log"]
TASKS = parse_tracker(TRACKER).tasks


def names(tasks) -> list[str]:
    return sorted(clip_name(t.label) for t in tasks)


class TrackerReadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        (self.root / "daily").mkdir()
        self.tracker = self.root / "daily" / f"{DAY}-tracker.md"
        self.tracker.write_text(TRACKER)

    def run_cli(self, *args: str, date: str | None = DAY) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--root", str(self.root), *(["--date", date] if date else []), *args]
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = tracker_read.main(argv)
            except SystemExit as stop:  # argparse refuses an argument by exiting
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def rows(self, *args: str) -> list[dict]:
        code, out, err = self.run_cli("tasks", *args)
        self.assertEqual(code, 0, err)
        return [] if out.strip() == "[]" else toon_decode(out)

    # Field report, 0.56.0: "three rounds of hand-rolled sed and awk. They found the sections, read the Resume block, and listed the open tasks"
    # Eval finding on #46: the coordinator ran `grep -n "^## " <tracker>` for each heading's line number, to Read that region.
    def test_sections_prints_each_headings_line_number_and_name_in_file_order(self):
        code, out, err = self.run_cli("sections")
        self.assertEqual(code, 0, err)
        lines = TRACKER.splitlines()
        for line in out.splitlines():
            self.assertRegex(line, r"^\d+ \S", "each line is `<line number> <name>`")
        printed = [line.split(" ", 1) for line in out.splitlines()]
        for number, name in printed:
            with self.subTest(heading=name):
                # 1-based, as the Read tool and an editor count: the line at that number is the heading itself.
                self.assertEqual(lines[int(number) - 1], f"## {name}")
        self.assertEqual([name for _, name in printed], [line[3:] for line in lines if line.startswith("## ")])

    def test_every_name_sections_prints_can_be_passed_to_section(self):
        _, out, _ = self.run_cli("sections")
        names = [line.partition(" ")[2] for line in out.splitlines()]
        self.assertEqual(len(names), len(HEADINGS))
        for name in names:
            with self.subTest(section=name):
                code, body, err = self.run_cli("section", name)
                self.assertEqual(code, 0, err)
                self.assertEqual(body.strip("\n"), "\n".join(section(TRACKER, f"## {name}")).strip("\n"))

    # Field report, 0.56.0: "`chief-of-stuff tracker --section Resume` and `--tasks open` (and one for Sessions) would make each read one call"
    def test_section_prints_the_body_as_written(self):
        for name in ("Resume", "File ownership", "Sessions"):
            with self.subTest(section=name):
                code, out, err = self.run_cli("section", name)
                self.assertEqual(code, 0, err)
                self.assertEqual(out.strip("\n"), "\n".join(section(TRACKER, f"## {name}")).strip("\n"))

    # Eval finding on #46: the coordinator ran `head -8 <tracker>` for the lines above the first heading, which are no section.
    def test_header_prints_the_lines_above_the_first_heading_as_written(self):
        code, out, err = self.run_cli("header")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip("\n"), TRACKER.split("\n## ", 1)[0].strip("\n"))
        self.assertIn("Coordinator: relay-12. Board: none.", out)
        self.assertNotIn("## ", out)

    def test_header_of_a_tracker_that_starts_with_a_heading_is_empty(self):
        self.tracker.write_text(TRACKER[TRACKER.index("## Resume"):])
        code, out, err = self.run_cli("header")
        self.assertEqual((code, out.strip()), (0, ""), err)

    def test_a_section_that_does_not_exist_is_an_error_naming_it(self):
        code, out, err = self.run_cli("section", "Requirements")
        self.assertNotIn(code, (0, None))
        self.assertEqual(out.strip(), "")
        self.assertIn("Requirements", err)

    # Field evidence, 0.42.0: "147 non-done rows down to one line each: name, owner, state, stage, issue."
    def test_tasks_prints_exactly_the_five_fields(self):
        for row in self.rows():
            self.assertEqual(list(row), FIELDS)

    # Field evidence, 0.42.0: "The output has to be one line per task and must never print the item cell."
    def test_tasks_is_one_row_per_task(self):
        code, out, err = self.run_cli("tasks")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(toon_decode(out)), len(TASKS))
        # TOON spends one line on the header; no task takes a second line.
        self.assertLessEqual(len([line for line in out.splitlines() if line.strip()]), len(TASKS) + 1)

    # Field evidence, 0.42.0: "The output has to be one line per task and must never print the item cell."
    def test_the_item_cell_is_never_printed_beyond_a_nameless_rows_clipped_label(self):
        _, out, _ = self.run_cli("tasks")
        self.assertNotIn("ITEMWORD", out)
        self.assertNotIn(PROSE.strip(), out)
        # The one exception: a row with no name cell is called by its label, which is the item's own first words.
        nameless = next(t for t in TASKS if "EARLYMARK" in t.item)
        self.assertFalse(nameless.name.strip())
        row = next(r for r in toon_decode(out) if "EARLYMARK" in r["name"])
        self.assertEqual(row["name"], clip_name(nameless.label))
        self.assertLessEqual(len(row["name"].rstrip("…")), SHORT_NAME)
        self.assertNotIn("LATEMARK", out)

    def test_name_is_the_label_clipped_by_clip_name(self):
        rows = self.rows()
        self.assertEqual(sorted(row["name"] for row in rows), names(TASKS))
        # One name is written over the cap and one row has no name cell: both are real names, neither the item.
        self.assertTrue(any(len(t.label) > len(clip_name(t.label)) for t in TASKS))
        self.assertTrue(any(not t.name.strip() for t in TASKS))

    # Third field report: "It breaks on any column reorder or a `|` inside a cell, and it bypasses the parser the scripts already share."
    def test_a_pipe_escaped_or_in_backticks_does_not_shift_columns(self):
        row = next(r for r in self.rows() if r["name"] == "Report readiness")
        self.assertEqual((row["owner"], row["state"], row["stage"], row["issue"]), ("worker-c", "orphaned", "fix", "#11"))

    def test_state_and_issue_are_the_whole_cell_not_the_kind(self):
        rows = {r["name"]: r for r in self.rows()}
        self.assertEqual(rows["Audit the upload endpoint"]["state"], "running 09:30")
        self.assertEqual(rows["Ship the changelog"]["state"], "done 08:10–08:55")
        self.assertEqual(rows["Draft release notes"]["issue"], f"[#9]({HOST}/team/app/-/issues/9)")
        self.assertEqual(sorted(r["issue"] for r in rows.values()), sorted(t.issue.strip() for t in TASKS))

    def test_rows_come_out_in_file_order(self):
        order = [clip_name(t.label) for t in TASKS]
        self.assertNotEqual(order, sorted(order), "the fixture is not already sorted")
        self.assertEqual([r["name"] for r in self.rows()], order)

    def test_state_and_not_together_intersect(self):
        self.assertEqual(sorted(r["name"] for r in self.rows("--state", "open", "--state", "running", "--not", "running")),
                         names(t for t in TASKS if t.kind == "open"))

    # Review of #46: without it `tasks --not done` silently drops or mislabels a running task.
    def test_every_row_the_parser_warns_about_is_named_on_stderr_whether_or_not_it_is_printed(self):
        self.tracker.write_text(WARNED)
        tasks = parse_tracker(WARNED).tasks
        warned = [t for t in tasks if t.warning]
        self.assertEqual(sorted(t.kind for t in warned), ["done", "running"])
        long = next(t for t in warned if t.kind == "running")
        self.assertGreater(len(long.label), len(clip_name(long.label)), "one warned label is longer than the clip")
        for args, kept in (((), lambda t: True), (("--not", "done"), lambda t: t.kind != "done"),
                           (("--state", "done"), lambda t: t.kind == "done"), (("--state", "waiting"), lambda t: t.kind == "waiting")):
            with self.subTest(args=args):
                code, out, err = self.run_cli("tasks", *args)
                self.assertEqual(code, 0, err)
                # A warned row is still a row: it is printed when the filter keeps it, under the name the parser read.
                self.assertEqual([r["name"] for r in toon_decode(out)], [clip_name(t.label) for t in tasks if kept(t)])
                # One line per warned row, before any filter, carrying the name stdout uses and the parser's warning.
                lines = err.splitlines()
                self.assertEqual(len(lines), len(warned), err)
                for task, line in zip(warned, lines):
                    self.assertIn(clip_name(task.label), line)
                    self.assertIn(task.warning, line)
                # A line is as short as a row: never the unclipped label, never the item.
                self.assertNotIn(long.label, err)
                for task in warned:
                    self.assertNotIn(task.item.strip(), err)

    def test_a_tracker_with_no_warned_row_prints_nothing_on_stderr(self):
        self.assertFalse([t for t in TASKS if t.warning])
        code, _, err = self.run_cli("tasks")
        self.assertEqual((code, err), (0, ""))

    def test_cells_are_printed_as_the_parser_read_them(self):
        row = next(r for r in self.rows() if r["name"] == "Audit the upload endpoint")
        self.assertEqual((row["owner"], row["stage"], row["issue"]), ("worker-a", "implement", "#7"))
        self.assertEqual(row["state"].split()[0], "running")

    # Third field report: "The coordinator listed non-done tasks with owner, state and issue by chaining two `awk` passes over the Tasks section"
    def test_not_leaves_out_tasks_of_that_state_kind(self):
        self.assertEqual(sorted(r["name"] for r in self.rows("--not", "done")),
                         names(t for t in TASKS if t.kind != "done"))

    def test_not_is_repeatable(self):
        self.assertEqual(sorted(r["name"] for r in self.rows("--not", "done", "--not", "running")),
                         names(t for t in TASKS if t.kind not in ("done", "running")))

    # Second field report: "It wanted the rows it could act on: open or unassigned, orphaned without a tree, and blocked. A `--state` filter on this read would cover that."
    def test_state_keeps_only_tasks_of_that_state_kind(self):
        for kind in ("open", "waiting", "orphaned", "running", "done"):
            with self.subTest(state=kind):
                wanted = [t for t in TASKS if t.kind == kind]
                self.assertTrue(wanted, "the fixture has a task of every kind")
                self.assertEqual(sorted(r["name"] for r in self.rows("--state", kind)), names(wanted))

    def test_state_is_repeatable(self):
        self.assertEqual(sorted(r["name"] for r in self.rows("--state", "open", "--state", "orphaned")),
                         names(t for t in TASKS if t.kind in ("open", "orphaned")))

    def test_a_blank_state_reads_as_open(self):
        blank = next(t for t in TASKS if not t.state.strip())
        self.assertIn(clip_name(blank.label), [r["name"] for r in self.rows("--state", "open")])
        self.assertNotIn(clip_name(blank.label), [r["name"] for r in self.rows("--not", "open")])

    def test_an_unknown_state_is_an_error_not_an_empty_list(self):
        for flag in ("--state", "--not"):
            with self.subTest(flag=flag):
                code, out, err = self.run_cli("tasks", flag, "blocked")
                self.assertNotIn(code, (0, None))
                self.assertEqual(out.strip(), "")
                self.assertIn("blocked", err)

    def test_an_empty_result_prints_an_empty_list(self):
        self.tracker.write_text(TRACKER.replace("| orphaned |", "| waiting |"))
        code, out, err = self.run_cli("tasks", "--state", "orphaned")
        self.assertEqual((code, out.strip()), (0, "[]"), err)

    # Field report, 21:26: "the coordinator ran grep on the tracker to find one Tasks row by its issue number."
    def test_issue_keeps_the_task_with_that_ref_in_every_form_kanban_accepts(self):
        home = read_config(self.root).backlog
        for arg in ("7", "#7", f"{HOST}/team/app/-/issues/7", "9", f"[#9]({HOST}/team/app/-/issues/9)", "other/repo#7"):
            with self.subTest(issue=arg):
                wanted = backlog.parse_issue_arg(arg, home)
                self.assertIsNotNone(wanted)
                # The ref decides, not the number: `#7` in the Backlog and `other/repo#7` are two issues.
                expected = [t for t in TASKS if t.issue.strip() and backlog.issue_ref(t.issue, home) == wanted]
                self.assertEqual(len(expected), 1)
                self.assertEqual([r["name"] for r in self.rows("--issue", arg)], names(expected))

    def test_an_issue_that_cannot_be_parsed_is_an_error(self):
        for arg in ("abc", "22 6", ""):
            with self.subTest(issue=arg):
                code, out, err = self.run_cli("tasks", "--issue", arg)
                self.assertNotIn(code, (0, None))
                self.assertEqual(out.strip(), "")
                self.assertTrue(err.strip())

    def test_a_workspace_config_error_is_a_message_on_stderr_not_a_traceback(self):
        for why, claude in (("no CLAUDE.md", None), ("no Coordinator block", "# Workspace\n\nNotes only.\n")):
            (self.root / "CLAUDE.md").unlink(missing_ok=True)
            if claude is not None:
                (self.root / "CLAUDE.md").write_text(claude)
            for args in (("header",), ("sections",), ("section", "Resume"), ("tasks",)):
                with self.subTest(why=why, args=args):
                    try:
                        code, out, err = self.run_cli(*args)
                    except Exception as exc:  # noqa: BLE001 — an uncaught error is the traceback this test forbids
                        self.fail(f"raised {exc!r}")
                    self.assertNotIn(code, (0, None))
                    self.assertEqual(out.strip(), "")
                    self.assertTrue(err.strip())
                    self.assertNotIn("Traceback", err)

    def test_a_missing_tracker_file_is_an_error_on_stderr_not_a_traceback(self):
        for args in (("header",), ("sections",), ("section", "Resume"), ("tasks",)):
            with self.subTest(args=args):
                try:
                    code, out, err = self.run_cli(*args, date="2026-01-01")
                except Exception as exc:  # noqa: BLE001 — an uncaught error is the traceback this test forbids
                    self.fail(f"raised {exc!r}")
                self.assertNotIn(code, (0, None))
                self.assertEqual(out.strip(), "")
                self.assertIn("2026-01-01", err)
                self.assertNotIn("Traceback", err)

    # Issue #46: "A read-only `chief-of-stuff tracker` command"
    def test_the_command_writes_nothing(self):
        before = (self.tracker.read_bytes(), self.tracker.stat().st_mtime_ns)
        files = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        for args in (("header",), ("sections",), ("section", "Resume"), ("section", "Nope"), ("tasks",),
                     ("tasks", "--not", "done"), ("tasks", "--state", "open"), ("tasks", "--issue", "#7")):
            self.run_cli(*args)
        self.assertEqual((self.tracker.read_bytes(), self.tracker.stat().st_mtime_ns), before)
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), files)

    def test_date_defaults_to_today_in_the_workspace_zone(self):
        # 03:30 UTC on the 26th is 22:30 on the 25th in America/Chicago. The fake's zoneless `now()` answers the
        # 26th as well, so a machine-local or a UTC default looks for a tracker that is not there, at any hour.
        instant = datetime(2026, 9, 26, 3, 30, tzinfo=timezone.utc)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

            @classmethod
            def utcnow(cls):
                return instant.replace(tzinfo=None)

        with mock.patch.object(tracker_read, "datetime", Clock):
            code, out, err = self.run_cli("section", "Log", date=None)
        self.assertEqual(code, 0, err)
        self.assertIn("opened the day", out)

    def test_section_keeps_an_indented_line_as_written(self):
        code, out, err = self.run_cli("section", "File ownership")
        self.assertEqual(code, 0, err)
        self.assertIn("\n  - tests under tests/a/ go with it", out)

    def test_sections_lists_only_second_level_headings(self):
        text = TRACKER.replace("\n## Log\n", "\n### Earlier\n\n- 08:00 a note\n\n#### Deeper\n\n## Log\n")
        self.tracker.write_text(text)
        code, out, err = self.run_cli("sections")
        self.assertEqual(code, 0, err)
        self.assertEqual([line.partition(" ")[2] for line in out.splitlines()], HEADINGS)

    def test_a_heading_with_trailing_spaces_is_read_by_the_name_sections_prints(self):
        self.tracker.write_text(TRACKER.replace("\n## Log\n", "\n## Log  \n"))
        _, out, _ = self.run_cli("sections")
        printed = [line.partition(" ")[2] for line in out.splitlines()]
        self.assertEqual([name.strip() for name in printed], HEADINGS)
        for name in printed:
            with self.subTest(section=name):
                code, body, err = self.run_cli("section", name)
                self.assertEqual(code, 0, err)
                self.assertEqual(body.strip("\n"), "\n".join(section(TRACKER, f"## {name.strip()}")).strip("\n"))

    def test_sections_numbers_count_newline_lines_only(self):
        # A form feed or U+2028 inside a line is one line to the Read tool and to an editor, and two to `str.splitlines`.
        text = (TRACKER.replace("Coordinator: relay-12.", "Coordinator: relay-12.\x0c")
                .replace("- Next: read", "- Next:\u2028read"))
        self.tracker.write_text(text, encoding="utf-8")
        code, out, err = self.run_cli("sections")
        self.assertEqual(code, 0, err)
        lines = text.split("\n")
        printed = [line.partition(" ") for line in out.splitlines()]
        self.assertEqual([name for _, _, name in printed], HEADINGS)
        for number, _, name in printed:
            with self.subTest(heading=name):
                self.assertEqual(lines[int(number) - 1], f"## {name}")


def launch_task(item: str, name: str = "Review change", issue: str = "workflow") -> Task:
    """A generic row; launch lookup needs no dated state or workspace configuration."""
    return Task(item, "w2", "open", "", "", "", name=name, issue=issue)


class GrownLaunchItemTest(unittest.TestCase):
    """#455: a note appended after dispatch preserves the launch's own row."""

    PROMPT = "Review the parser change and check empty input handling."
    NOTE = " Reviewed 15:09 by w2: the empty input checks pass."

    def assert_lookup(self, prompt: str, tasks: list[Task], expected: Task | None):
        # Exercise both the reusable index and the public one-off lookup, by identity.
        for label, lookup in (("launcher", launcher(tasks)), ("launch_row", lambda text: launch_row(text, tasks))):
            with self.subTest(lookup=label):
                row, name = lookup(prompt)
                self.assertIs(row, expected)
                self.assertEqual(name, expected.name if expected else prompt.split("\n", 1)[0][:80])

    def test_a_launch_finds_its_item_after_a_note_was_appended(self):
        row = launch_task(self.PROMPT + self.NOTE)
        self.assert_lookup(self.PROMPT, [row], row)

    def test_exact_or_shrunk_items_win_before_grown_items(self):
        grown = launch_task(self.PROMPT + self.NOTE, "Later grown row")
        # The grown-only subcase is red today; exact and shrunk subcases guard the old first rung.
        for label, item in (("grown only", None), ("exact", self.PROMPT), ("shrunk", "Review the parser change")):
            with self.subTest(item=label):
                first = launch_task(item, "Original row") if item else None
                self.assert_lookup(self.PROMPT, [first, grown] if first else [grown], first or grown)

    def test_a_grown_item_wins_before_a_later_row_sharing_the_issue(self):
        issue_url = "https://forge.example/group/project/-/issues/7"
        for issue in ("#7", issue_url):
            with self.subTest(issue=issue):
                prompt = f"Review the parser change for {issue} and check empty input handling."
                own = launch_task(prompt + self.NOTE, "Review parser", issue)
                later = launch_task("Verify the separate parser checks", "Later verify", issue)
                self.assert_lookup(prompt, [own, later], own)

    def test_the_latest_of_several_grown_items_sharing_the_launch_prefix_wins(self):
        first = launch_task(self.PROMPT + " Built 14:37 by w1: checks pass.", "First pass")
        latest = launch_task(self.PROMPT + self.NOTE, "Latest pass")
        # Lexicographic order and tracker order disagree; position must decide the tie.
        for tasks in ([first, latest], [latest, first]):
            with self.subTest(latest=tasks[-1].name):
                self.assert_lookup(self.PROMPT, tasks, tasks[-1])

    def test_growth_matching_requires_at_least_40_characters(self):
        # #455 leaves the cutoff open: 40 characters keeps a generic short instruction from claiming a row.
        unrelated = launch_task("Review the unrelated release checklist and publish the notes.")
        self.assert_lookup("Review", [unrelated], None)
        boundary = "Review the parser change for empty input"
        self.assertEqual(len(boundary), 40)
        for width in (39, 40):
            with self.subTest(characters=width):
                prompt = boundary[:width]
                grown = launch_task(prompt + self.NOTE)
                self.assert_lookup(prompt, [grown], grown if width == 40 else None)

    def test_growth_cannot_extend_a_change_number_into_a_different_change(self):
        prompt = "Review https://forge.example/group/project/-/merge_requests/5"
        row = launch_task(prompt + "0 and check the parser diagnostics", "Review change 50")
        self.assertGreaterEqual(len(prompt), 40)
        self.assert_lookup(prompt, [row], None)

    def test_growth_remainder_must_start_at_whitespace_or_punctuation(self):
        for remainder in (" and check diagnostics", "; check diagnostics", ". Checks pass", "\nChecks pass",
                          "x and check diagnostics"):
            with self.subTest(remainder=remainder):
                row = launch_task(self.PROMPT + remainder)
                self.assert_lookup(self.PROMPT, [row], None if remainder[0] == "x" else row)

    def test_grown_matches_with_different_names_and_issue_keys_are_ambiguous(self):
        first = launch_task(self.PROMPT + " Check the parser diagnostics.", "Review parser", "#7")
        second = launch_task(self.PROMPT + " Check the formatter diagnostics.", "Review formatter", "#8")
        for tasks in ([first, second], [second, first]):
            with self.subTest(order=[t.name for t in tasks]):
                self.assert_lookup(self.PROMPT, tasks, None)

    def test_a_long_boilerplate_prefix_shared_by_unrelated_tasks_matches_none(self):
        prompt = "Review the change and report every finding to the coordinator for the next pass."
        self.assertGreaterEqual(len(prompt), 40)
        tasks = [launch_task(prompt + suffix, name, issue) for suffix, name, issue in (
            (" Check the parser diagnostics.", "Review parser", "#7"),
            (" Check the formatter diagnostics.", "Review formatter", "#8"),
            (" Check the decoder diagnostics.", "Review decoder", "#9"))]
        for ordered in (tasks, list(reversed(tasks))):
            with self.subTest(order=[t.name for t in ordered]):
                self.assert_lookup(prompt, ordered, None)

    def test_several_grown_rows_sharing_one_issue_key_still_choose_the_latest(self):
        url = "https://forge.example/group/project/-/issues/7"
        # Different cell spellings of the same canonical issue key are still one task's passes.
        first = launch_task(self.PROMPT + " Built by w1: checks pass.", "First pass", url)
        second = launch_task(self.PROMPT + self.NOTE, "Latest pass", f"[#7]({url})")
        for tasks in ([first, second], [second, first]):
            with self.subTest(latest=tasks[-1].name):
                self.assert_lookup(self.PROMPT, tasks, tasks[-1])

    def test_several_grown_rows_sharing_one_name_still_choose_the_latest(self):
        # The rejection rule requires BOTH different names and different issue keys.
        first = launch_task(self.PROMPT + " Built by w1: checks pass.", "Review parser", "#7")
        second = launch_task(self.PROMPT + self.NOTE, "Review parser", "#8")
        for tasks in ([first, second], [second, first]):
            with self.subTest(latest=tasks[-1].issue):
                self.assert_lookup(self.PROMPT, tasks, tasks[-1])


class ChangeKeysReviewTest(unittest.TestCase):
    """R3: a closing issue belongs to the item's own single change, never a context reference."""

    CHANGE = "https://forge.example/group/project/-/merge_requests/51"
    OTHER_CHANGE = "https://forge.example/group/project/-/merge_requests/50"
    CONTEXT_ISSUE = "https://forge.example/group/project/-/issues/7"
    OWN_ISSUE = "gitlab:forge.example/group/project#8"

    def test_a_context_issue_url_is_not_a_change_issue_key(self):
        text = f"Review {self.CHANGE} and check diagnostics; context: {self.CONTEXT_ISSUE}"
        self.assertEqual(change_keys(text), ("", self.CHANGE))

    def test_a_closing_clause_before_the_change_url_is_not_its_issue_key(self):
        # PR 461 R1: only the suffix after the change URL supplies its issue clause.
        for change in (self.CHANGE, "https://github.com/group/project/pull/51"):
            with self.subTest(change=change):
                text = f"Earlier work closes #9; review {change} and check diagnostics"
                self.assertEqual(change_keys(text), ("", change))

    def test_the_changes_own_closing_clause_wins_over_a_later_context_issue_url(self):
        for verb in ("closes", "fixes", "issue"):
            with self.subTest(verb=verb):
                text = f"Review {self.CHANGE}; {verb} #8; context: {self.CONTEXT_ISSUE}"
                self.assertEqual(change_keys(text), (self.OWN_ISSUE, self.CHANGE))

    def test_rebase_onto_one_change_then_review_another_is_unkeyed(self):
        # P3: silently taking 50 would key a review of 51 to the rebase target's task.
        text = f"Rebase onto {self.OTHER_CHANGE} then review {self.CHANGE}"
        self.assertEqual(change_keys(text), ("", ""))

    def test_two_distinct_changes_are_unkeyed_even_with_a_closing_clause(self):
        for first, second in ((self.CHANGE, self.OTHER_CHANGE), (self.OTHER_CHANGE, self.CHANGE)):
            with self.subTest(first=first):
                self.assertEqual(change_keys(f"Review {first}; closes #8; context: {second}"), ("", ""))

    def test_one_change_with_two_distinct_issues_is_unkeyed_in_either_order(self):
        for first, second in ((7, 8), (8, 7)):
            with self.subTest(first=first):
                self.assertEqual(change_keys(f"Review {self.CHANGE}; closes #{first}; fixes #{second}"), ("", ""))

    def test_one_change_with_its_own_issue_clause_keeps_the_existing_key(self):
        for verb in ("closes", "fixes", "issue", "resolves"):
            with self.subTest(verb=verb):
                self.assertEqual(change_keys(f"Review {self.CHANGE}; {verb} #8"), (self.OWN_ISSUE, self.CHANGE))

    def test_repeated_references_to_the_same_change_and_issue_are_not_ambiguous(self):
        text = f"Review {self.CHANGE}; closes #8; verify {self.CHANGE}; issue #8"
        self.assertEqual(change_keys(text), (self.OWN_ISSUE, self.CHANGE))


if __name__ == "__main__":
    unittest.main()

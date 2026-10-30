"""Unit tests for scripts/migrate_backlog.py: the triage is the expensive half, not the moving.

The tracker's `## Issues` rows are six hours stale by the time they are read — several closed
themselves and at least two were wrong about their own state when the coordinator audited at 04:00.
So nothing here decides. It proposes, the owner corrects, and only what he approves is created.

The row's own key is a hash of its text, which makes the report and the migration agree about
*which row* even after the tracker moves — and makes a row that changed since the report
unmigratable rather than silently migrated as something else.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import migrate_backlog as mb  # noqa: E402

HEAD = "| item | owner | state | since | due | size | checklist |\n|---|---|---|---|---|---|---|\n"


def tracker(*rows: str) -> Path:
    body = "# Tracker\n\n## Tasks\n\n" + HEAD + "| a task | the owner | open | 2026-09-19 |  | M | — |\n"
    body += "\n## Issues\n\n" + HEAD + "".join(rows) + "\n## Decisions\n\n"
    path = Path(tempfile.mkdtemp()) / "2026-09-19-tracker.md"
    path.write_text(body)
    return path


def row(item: str, owner: str = "unassigned", state: str = "open", since: str = "2026-09-19",
        size: str = "S") -> str:
    return f"| {item} | {owner} | {state} | {since} |  | {size} | — |\n"


class ReadTest(unittest.TestCase):
    def test_reads_the_issues_section_and_not_the_tasks_section(self):
        got = mb.issue_rows(tracker(row("a defect"), row("another defect")))
        self.assertEqual([i.title for i in got], ["a defect", "another defect"])

    def test_a_pipe_inside_a_code_span_does_not_split_the_row(self):
        """render_board's splitter is mark-aware — finding 100. A second splitter would lose that."""
        got = mb.issue_rows(tracker(row("the filter `a | b` is wrong")))
        self.assertEqual(len(got), 1)
        self.assertIn("a | b", got[0].item)

    def test_the_key_is_stable_for_the_same_text_and_moves_with_it(self):
        one = mb.issue_rows(tracker(row("a defect")))[0]
        again = mb.issue_rows(tracker(row("a defect")))[0]
        changed = mb.issue_rows(tracker(row("a defect, edited")))[0]
        self.assertEqual(one.key, again.key)
        self.assertNotEqual(one.key, changed.key)


class ProposeTest(unittest.TestCase):
    def test_an_open_unowned_row_is_proposed_for_the_move(self):
        one = mb.issue_rows(tracker(row("a defect")))[0]
        self.assertEqual(mb.propose(one).disposition, mb.MOVE)

    def test_a_done_row_is_already_closed_here(self):
        one = mb.issue_rows(tracker(row("a defect", state="done 21:16")))[0]
        self.assertEqual(mb.propose(one).disposition, mb.CLOSED)

    def test_a_row_whose_prose_disputes_its_state_is_flagged_for_a_human(self):
        """The coordinator measured two of these at 04:00. A confident disposition would carry them in."""
        one = mb.issue_rows(tracker(row("**FINISHED 00:12** and on main", state="open")))[0]
        got = mb.propose(one)
        self.assertEqual(got.disposition, mb.CHECK)
        self.assertIn("prose", got.why)

    def test_an_owned_running_row_is_not_backlog(self):
        """the owner, 22:43: what is being worked stays in the tracker. Only what is parked moves."""
        one = mb.issue_rows(tracker(row("a defect", owner="openemr-impl-1", state="running 03:20")))[0]
        self.assertEqual(mb.propose(one).disposition, mb.KEEP)

    def test_every_proposal_says_why(self):
        for state in ("open", "done 21:16", "waiting", "running 03:20"):
            one = mb.issue_rows(tracker(row("a defect", state=state)))[0]
            self.assertTrue(mb.propose(one).why, state)


class ReportTest(unittest.TestCase):
    def test_the_report_carries_one_editable_row_per_issue(self):
        text = mb.report(mb.proposals(mb.issue_rows(tracker(row("a defect"), row("b defect")))))
        rows = [l for l in text.splitlines() if mb.ROW.match(l.strip())]  # the legend rows also start "| `"
        self.assertEqual(len(rows), 2)
        self.assertIn("a defect", text)

    def test_the_report_names_the_dispositions_a_human_may_write(self):
        text = mb.report(mb.proposals(mb.issue_rows(tracker(row("a defect")))))
        for word in (mb.MOVE, mb.KEEP, mb.CLOSED, mb.CHECK):
            self.assertIn(word, text)

    def test_reading_the_report_back_returns_what_a_human_wrote(self):
        issues = mb.issue_rows(tracker(row("a defect"), row("b defect")))
        text = mb.report(mb.proposals(issues))
        edited = text.replace(f"| `{issues[0].key}` | {mb.MOVE}", f"| `{issues[0].key}` | {mb.KEEP}")
        got = mb.read_report(edited)
        self.assertEqual(got[issues[0].key], mb.KEEP)
        self.assertEqual(got[issues[1].key], mb.MOVE)


class DriftTest(unittest.TestCase):
    """The tracker moves while the owner reads the report. Per-row keys catch a changed row; the file
    hash catches the case where he is reading a report of a tracker that has since moved on."""

    def test_the_report_records_the_tracker_it_read(self):
        path = tracker(row("a defect"))
        text = mb.report(mb.proposals(mb.issue_rows(path)), source=str(path),
                         fingerprint=mb.fingerprint(path))
        self.assertIn(mb.fingerprint(path), text)

    def test_a_tracker_that_moved_since_the_report_is_noticed(self):
        path = tracker(row("a defect"))
        before = mb.fingerprint(path)
        path.write_text(path.read_text() + row("a later defect"))
        self.assertNotEqual(mb.fingerprint(path), before)
        self.assertEqual(mb.read_fingerprint(f"tracker `{path}` at `{before}`"), before)

    def test_a_report_with_no_fingerprint_reads_as_none(self):
        self.assertEqual(mb.read_fingerprint("no hash here"), "")


class MigrateTest(unittest.TestCase):
    def test_only_rows_marked_move_are_created(self):
        issues = mb.issue_rows(tracker(row("a defect"), row("b defect")))
        decided = {issues[0].key: mb.MOVE, issues[1].key: mb.KEEP}
        made = []
        got = mb.migrate(issues, decided, make=lambda title, body, labels: made.append(title))
        self.assertEqual(made, ["a defect"])
        self.assertEqual(len(got.created), 1)
        self.assertEqual(len(got.skipped), 1)

    def test_a_row_the_report_never_saw_is_never_created(self):
        """No disposition is not consent. A row edited after the report gets a new key and stops here."""
        issues = mb.issue_rows(tracker(row("a defect")))
        made = []
        got = mb.migrate(issues, {"deadbeef": mb.MOVE}, make=lambda *a: made.append(a))
        self.assertEqual(made, [])
        self.assertEqual(len(got.unknown), 1)

    def test_the_body_carries_the_row_and_says_where_it_came_from(self):
        issues = mb.issue_rows(tracker(row("a defect with **detail**", owner="sam", state="waiting")))
        bodies = []
        mb.migrate(issues, {issues[0].key: mb.MOVE}, make=lambda t, b, l: bodies.append(b),
                   source="Areas/daily/2026-09-19-tracker.md")
        self.assertIn("a defect with **detail**", bodies[0])
        self.assertIn("2026-09-19-tracker.md", bodies[0])
        self.assertIn("sam", bodies[0])

    def test_nothing_is_created_when_no_row_is_approved(self):
        issues = mb.issue_rows(tracker(row("a defect")))
        made = []
        got = mb.migrate(issues, {issues[0].key: mb.CHECK}, make=lambda *a: made.append(a))
        self.assertEqual(made, [])
        self.assertEqual(got.created, ())


if __name__ == "__main__":
    unittest.main()

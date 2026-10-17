"""tracker_log.py: each Log line the scripts write reads back with the pattern its readers use, worded as it always was."""

import sys
import unittest
from datetime import date, time
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tracker_log as tl  # noqa: E402

CT = ZoneInfo("America/Chicago")
DAY = date(2026, 9, 18)
TREE = "worktree `trees/up` (fix-upload)"  # as one_shot._tree_note names a tree in File ownership


class RoundTripTest(unittest.TestCase):
    """Format a line, then read its fields back."""

    def test_a_stage_line(self):
        line = tl.stage_line("11:05", "Upload path check", "pr")
        self.assertEqual(tl.STAGE.match(line).groups(), ("11", "05", "Upload path check", "pr"))

    def test_a_started_line(self):
        line = tl.started_line("09:15", "worker01", "codex", "gpt-5", TREE, "Security audit")
        self.assertEqual(tl.STARTED.match(line).groups(), ("09", "15", "worker01", "Security audit"))
        self.assertEqual(tl.STARTED_DETAIL.match(line).groups(),
                         ("09:15", "worker01", "codex gpt-5", "trees/up", "Security audit"))
        bare = tl.started_line("09:15", "worker01", "claude", "", TREE, "Security audit")
        self.assertEqual(tl.STARTED_DETAIL.match(bare)[3], "claude")

    def test_a_started_line_with_an_effort_after_the_model_is_still_read(self):
        # (#52) What the pages read: STARTED (workers page, moves) and STARTED_DETAIL (issue page) both match it,
        # and the time, name, tree and task come out as without an effort.
        line = "- 09:15 one-shot worker01 started: codex gpt-test effort=medium, worktree `trees/up`, task Security audit"
        self.assertEqual(tl.STARTED.match(line).groups(), ("09", "15", "worker01", "Security audit"))
        got = tl.STARTED_DETAIL.match(line)
        self.assertEqual((got[1], got[2], got[4], got[5]), ("09:15", "worker01", "trees/up", "Security audit"))
        moves = tl.moves(f"## Log\n\n{line}\n", DAY, CT)
        self.assertEqual([(m.at.strftime("%H:%M"), m.name, m.stage, m.launch, m.worker) for m in moves],
                         [("09:15", "Security audit", "implement", True, "worker01")])

    def test_an_ended_line(self):
        for status, outcome in (("done", tl.COMPLETED), ("human_review", tl.HUMAN_REVIEW)):
            with self.subTest(status=status):
                line = tl.ended_line("10:20", "worker01", status, "fixed the cap", "a.py b.py")
                self.assertEqual(tl.ENDED.match(line).groups(), ("10", "20", "worker01", outcome))
                self.assertIsNone(tl.RELAUNCH.match(line))

    def test_a_relaunch_line(self):
        line = tl.relaunch_line("10:20", "worker01", "Security audit", "target PR merged before work began")
        self.assertEqual(tl.RELAUNCH.match(line).groups(), ("10", "20", "worker01", "relaunch"))
        self.assertIsNone(tl.ENDED.match(line))
        # The launcher finds an earlier relaunch of the task by this text.
        self.assertIn(tl.RELAUNCHED.format(task="Security audit"), line)

    def test_a_relaunch_line_does_not_double_a_reasons_final_period(self):
        # #436: a quota stop's reset text ends in a period of its own.
        marker = tl.RELAUNCHED.format(task="Security audit")
        for reason, tail in (("Resets in 3h46m46s.", "Resets in 3h46m46s."), ("base moved", "base moved."), ("", ".")):
            with self.subTest(reason=reason):
                self.assertEqual(tl.relaunch_line("10:20", "worker01", "Security audit", reason),
                                 f"- 10:20 one-shot worker01{marker}{tail}")

    def test_a_relaunch_line_keeps_a_reason_that_ends_in_several_periods_unchanged(self):
        marker = tl.RELAUNCHED.format(task="Security audit")
        for reason in ("waiting..", "waiting..."):
            with self.subTest(reason=reason):
                self.assertEqual(tl.relaunch_line("10:20", "worker01", "Security audit", reason),
                                 f"- 10:20 one-shot worker01{marker}{reason}")

    def test_a_timed_out_line(self):
        # #110: the first timeout's marker names the worker and the wait that ran out.
        line = tl.timed_out_line("10:20", "worker01", 30, "Security audit")
        self.assertEqual(tl.TIMED_OUT.match(line).groups(), ("10", "20", "worker01", "30", "Security audit"))
        self.assertIsNone(tl.ENDED.match(line))
        self.assertIsNone(tl.RELAUNCH.match(line))

    def test_an_ended_line_does_not_double_a_reasons_final_period(self):
        for reason, tail in (("Resets in 3h46m46s.", "Resets in 3h46m46s."), ("fixed the cap", "fixed the cap."), ("", ".")):
            with self.subTest(reason=reason):
                line = tl.ended_line("10:20", "worker01", "human_review", reason, "a.py")
                self.assertEqual(line, f"- 10:20 one-shot worker01: {tl.HUMAN_REVIEW} — {tail} Changes: a.py.")

    def test_the_moves_read_from_the_written_lines(self):
        text = "\n".join(("# Tracker", "", "## Log", "",
                          tl.started_line("09:15", "worker01", "codex", "", TREE, "Security audit"),
                          tl.stage_line("09:40", "Upload path check", "review"),
                          tl.ended_line("10:20", "worker01", "done", "fixed", "a.py"),
                          tl.started_line("10:30", "worker02", "claude", "opus", TREE, "Cap uploads"),
                          tl.ended_line("11:00", "worker02", "human_review", "unclear", "b.py"),
                          tl.relaunch_line("11:10", "worker01", "Security audit", "base moved"), ""))
        self.assertEqual([(m.at.time(), m.name, m.stage, m.launch, m.worker) for m in tl.moves(text, DAY, CT)], [
            (time(9, 15), "Security audit", "implement", True, "worker01"),
            (time(9, 40), "Upload path check", "review", False, ""),
            (time(10, 20), "Security audit", "pr", True, "worker01"),
            (time(10, 30), "Cap uploads", "implement", True, "worker02"),
            (time(11, 0), "Cap uploads", "review", True, "worker02")])

    def test_a_hold_failed_line_reads_back_its_task_and_error(self):
        # #566: the launcher records the hold it could not apply; the audit reads the task and reason back.
        line = tl.hold_failed_line("10:20", "worker01", "Security audit",
                                   "missing label in GitHub: !! - HUMAN REVIEW REQUIRED")
        self.assertEqual(tl.HOLD_FAILED.match(line).groups(),
                         ("10", "20", "worker01", "missing label in GitHub: !! - HUMAN REVIEW REQUIRED", "Security audit"))
        for pattern in (tl.STAGE, tl.STARTED, tl.ENDED, tl.RELAUNCH):
            self.assertIsNone(pattern.match(line), line)
        text = "\n".join(("# Tracker", "", "## Log", "", line, ""))
        self.assertEqual(tl.hold_failures(text),
                         {"Security audit": "missing label in GitHub: !! - HUMAN REVIEW REQUIRED"})
        self.assertEqual(tl.moves(text, DAY, CT), [])

    def test_the_last_hold_failure_for_a_task_wins(self):
        first = tl.hold_failed_line("10:20", "worker01", "Security audit", "missing label in GitHub: X")
        second = tl.hold_failed_line("11:30", "worker02", "Security audit", "lab was down")
        text = "\n".join(("# Tracker", "", "## Log", "", first, second, ""))
        self.assertEqual(tl.hold_failures(text), {"Security audit": "lab was down"})

    def test_an_approved_forge_closure_line_reads_back_its_task(self):
        # #547: the coordinator records the user's approval; the audit does not reopen the task it names.
        line = tl.approved_close_line("10:05", "Open branch work",
                                      "the forge closed its issue; work stays on the branch")
        self.assertEqual(tl.APPROVED_CLOSE.match(line).groups(),
                         ("10", "05", "the forge closed its issue; work stays on the branch", "Open branch work"))
        for pattern in (tl.STAGE, tl.STARTED, tl.ENDED, tl.RELAUNCH):
            self.assertIsNone(pattern.match(line), line)
        text = "\n".join(("# Tracker", "", "## Log", "", line, ""))
        self.assertEqual(tl.approved_closes(text), {"Open branch work"})
        self.assertEqual(tl.moves(text, DAY, CT), [])


class WordingTest(unittest.TestCase):
    """One line of each kind, byte for byte as `chief-of-stuff log --stage` and the one-shot launcher wrote it
    before they shared these formatters. Old trackers hold these lines, and the pages still read them."""

    def test_each_kind_is_worded_as_before(self):
        for line, before in (
            (tl.stage_line("11:05", "Upload path check", "pr"), "- 11:05 stage: Upload path check → pr"),
            (tl.started_line("09:15", "worker01", "codex", "gpt-5", TREE, "Security audit"),
             "- 09:15 one-shot worker01 started: codex gpt-5, worktree `trees/up`, task Security audit"),
            (tl.started_line("09:15", "worker01", "claude", "", "worktree `w` (w)", "Security audit"),
             "- 09:15 one-shot worker01 started: claude, worktree `w`, task Security audit"),
            (tl.ended_line("10:20", "worker01", "done", "fixed the cap", "a.py b.py"),
             "- 10:20 one-shot worker01: completed; awaiting integration — fixed the cap. Changes: a.py b.py."),
            (tl.ended_line("10:20", "worker01", "human_review", "needs a decision", "changed parser"),
             "- 10:20 one-shot worker01: HUMAN REVIEW NEEDED — needs a decision. Changes: changed parser."),
            (tl.relaunch_line("10:20", "worker01", "Security audit", "target PR merged before work began"),
             "- 10:20 one-shot worker01: relaunch requested for Security audit — target PR merged before work began."),
        ):
            with self.subTest(before=before):
                self.assertEqual(line, before)


class RelaunchStopTest(unittest.TestCase):
    """#457: the written relaunch request records the matched worker's stop time."""

    def test_a_relaunch_requested_stop_is_read_at_its_stop_time(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        stopped = tl.relaunch_line("10:20", "worker01", "Check parser", "base moved")
        text = f"## Log\n\n{started}\n{stopped}\n"
        # Assert the boundary and its identity without prescribing an internal stage name for a stop.
        self.assertEqual([(m.at.date(), m.at.time(), m.name, m.launch, m.worker, m.line)
                          for m in tl.moves(text, DAY, CT)], [
            (DAY, time(9, 15), "Check parser", True, "worker01", started),
            (DAY, time(10, 20), "Check parser", True, "worker01", stopped)])

    def test_an_unmatched_relaunch_does_not_stop_another_worker(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        stopped = tl.relaunch_line("10:20", "worker02", "Check parser", "base moved")
        text = f"## Log\n\n{started}\n{stopped}\n"
        self.assertEqual([(m.at.time(), m.name, m.stage, m.worker) for m in tl.moves(text, DAY, CT)],
                         [(time(9, 15), "Check parser", "implement", "worker01")])

    def test_prose_mentioning_a_relaunch_mid_line_is_not_a_stop(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        quoted = tl.relaunch_line("10:20", "worker01", "Check parser", "base moved")
        note = f"- 10:20 note: retry example {quoted} was discussed; work continues."
        text = f"## Log\n\n{started}\n{note}\n"
        self.assertIsNone(tl.RELAUNCH.search(note))
        self.assertEqual([m.line for m in tl.moves(text, DAY, CT)], [started])

    def test_a_stage_named_relaunch_is_read_as_a_stage_move(self):
        line = tl.stage_line("10:20", "Check parser", "relaunch")
        self.assertIsNone(tl.RELAUNCH.match(line))
        self.assertEqual([(m.at.time(), m.name, m.stage, m.launch, m.worker, m.line)
                          for m in tl.moves(f"## Log\n\n{line}\n", DAY, CT)],
                         [(time(10, 20), "Check parser", "relaunch", False, "", line)])

    def test_the_same_worker_relaunched_twice_records_only_its_first_stop(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        first = tl.relaunch_line("10:20", "worker01", "Check parser", "base moved")
        duplicate = tl.relaunch_line("10:30", "worker01", "Check parser", "base moved again")
        text = f"## Log\n\n{started}\n{first}\n{duplicate}\n"
        self.assertEqual([m.line for m in tl.moves(text, DAY, CT)], [started, first])

    def test_a_relaunch_before_any_launch_is_ignored(self):
        early = tl.relaunch_line("09:00", "worker01", "Check parser", "base moved")
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        text = f"## Log\n\n{early}\n{started}\n"
        self.assertEqual([m.line for m in tl.moves(text, DAY, CT)], [started])

    def test_a_relaunch_without_a_timestamp_is_ignored(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        unstamped = "- one-shot worker01: relaunch requested for Check parser — base moved."
        text = f"## Log\n\n{started}\n{unstamped}\n"
        self.assertEqual([m.line for m in tl.moves(text, DAY, CT)], [started])

    def test_a_relaunch_with_an_invalid_clock_is_ignored(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        for clock in ("24:00", "10:60"):
            with self.subTest(clock=clock):
                stopped = tl.relaunch_line(clock, "worker01", "Check parser", "base moved")
                text = f"## Log\n\n{started}\n{stopped}\n"
                self.assertEqual([m.line for m in tl.moves(text, DAY, CT)], [started])

    def test_a_relaunch_after_the_worker_ended_adds_no_move(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        stopped = tl.relaunch_line("10:30", "worker01", "Check parser", "base moved")
        for status in ("done", "human_review"):
            with self.subTest(status=status):
                ended = tl.ended_line("10:20", "worker01", status, "check finished", "parser.py")
                text = f"## Log\n\n{started}\n{ended}\n{stopped}\n"
                self.assertEqual([m.line for m in tl.moves(text, DAY, CT)], [started, ended])

    def test_a_relaunch_for_another_task_does_not_consume_the_workers_launch(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        wrong = tl.relaunch_line("10:20", "worker01", "Check formatter", "base moved")
        right = tl.relaunch_line("10:30", "worker01", "Check parser", "base moved")
        text = f"## Log\n\n{started}\n{wrong}\n{right}\n"
        self.assertEqual([m.line for m in tl.moves(text, DAY, CT)], [started, right])

    def test_a_longer_task_with_the_same_prefix_does_not_consume_the_launch(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        right = tl.relaunch_line("10:30", "worker01", "Check parser", "base moved")
        # Merely accepting the task prefix would consume the run before its real stop.
        for other_task in ("Check parser extra", "Check parser — extra"):
            with self.subTest(other_task=other_task):
                wrong = tl.relaunch_line("10:20", "worker01", other_task, "base moved")
                text = f"## Log\n\n{started}\n{wrong}\n{right}\n"
                self.assertEqual([m.line for m in tl.moves(text, DAY, CT)], [started, right])

    def test_stop_matching_uses_the_relaunch_formatters_task_suffix(self):
        started = tl.started_line("09:15", "worker01", "codex", "gpt-test", TREE, "Check parser")
        # Keep RELAUNCH's recognized prefix fixed, but vary the formatter's suffix to detect a
        # separately hand-written task delimiter in the reader. The unmodified template is a control.
        for template in (tl.RELAUNCHED, tl.RELAUNCHED.replace(" — ", " :: ")):
            with self.subTest(template=template), mock.patch.object(tl, "RELAUNCHED", template):
                stopped = f"- 10:20 one-shot worker01{tl.RELAUNCHED.format(task='Check parser')}base moved."
                self.assertIsNotNone(tl.RELAUNCH.match(stopped))
                got = tl.moves(f"## Log\n\n{started}\n{stopped}\n", DAY, CT)
                self.assertEqual([(m.at.time(), m.name, m.worker, m.line) for m in got], [
                    (time(9, 15), "Check parser", "worker01", started),
                    (time(10, 20), "Check parser", "worker01", stopped)])


if __name__ == "__main__":
    unittest.main()

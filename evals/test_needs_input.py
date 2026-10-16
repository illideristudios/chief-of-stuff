"""#210: one visible name for a person wait, including historical launcher outcomes."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"), str(Path(__file__).resolve().parent)]
import board_sources as bs
import columns
import decision_page as dp
import flow_chart as fc
import issue_page as ip
import render_board as rb
import tracker_log as tl
import workers_page as wp
from tracker import parse_tracker
from test_flow_chart import CT, LANES, NOW, TODAY, TODAY_TRACKER
from test_issue_page import page, section, text, today_tracker


class NeedsInputTest(unittest.TestCase):
    def test_a_waiting_card_keeps_its_machine_kind_and_shows_needs_input(self):
        tasks = parse_tracker(TODAY_TRACKER).tasks
        cards = [c for col in rb.build_columns(tasks, LANES) for c in col.cards]
        card = next(c for c in cards if c.name == "Cache warmup")
        self.assertEqual(card.kind, "waiting")
        self.assertEqual(card.state, "needs input")
        self.assertEqual(card.flag, columns.Mark("NEEDS INPUT", "hold"))

    def test_old_and_new_end_lines_stop_the_same_flow_run(self):
        start = tl.started_line("01:00", "impl-1", "codex", "test", "worktree `a` (a)", "Cap uploads")
        for outcome in ("NEEDS INPUT", "HUMAN REVIEW NEEDED"):
            with self.subTest(outcome=outcome):
                tracker = TODAY_TRACKER.replace("- 01:00 stage: Upload size limit → pr\n", "") + start + f"\n- 01:30 one-shot impl-1: {outcome} — choose a limit. Changes: none.\n"
                moves = tl.moves(tracker, TODAY, CT)
                ends = [m for m in moves if m.launch and m.stage == "review"]
                self.assertEqual([(m.worker, m.at.hour, m.at.minute) for m in ends], [("impl-1", 1, 30)])
                rows = fc.build(moves, parse_tracker(tracker).tasks, LANES, {}, {}, NOW)
                row = next(r for _, r in rows if r.ref == "#109")
                self.assertTrue(any(s.category == "review" for s in row.segments))

    def test_workers_normalizes_old_and_new_outcomes_and_counts_both(self):
        tracker = "## Log\n\n"
        for n, outcome in enumerate(("NEEDS INPUT", "HUMAN REVIEW NEEDED")):
            tracker += f"- 01:00 one-shot worker{n} started: claude, worktree `a`, task Work {n}\n"
            tracker += f"- 01:30 one-shot worker{n}: {outcome} — choose a limit. Changes: none.\n"
        with tempfile.TemporaryDirectory() as tmp:
            html = wp.render(tracker, bs.EMPTY, NOW, Path(tmp), 4)
        self.assertIn('<b>2</b><span class="cap">needs input</span>', html)
        self.assertEqual(html.count('<td>needs input</td>'), 2)
        self.assertNotIn('human review', html)

    def test_task_page_normalizes_the_outcome_in_workers_and_history(self):
        tracker = today_tracker()
        for outcome in ("NEEDS INPUT", "HUMAN REVIEW NEEDED"):
            with self.subTest(outcome=outcome):
                tracker = tracker.replace("HUMAN REVIEW NEEDED", outcome).replace("NEEDS INPUT", outcome)
                html = page(today=tracker)
                self.assertIn('needs input', text(section(html, 'Workers')))
                self.assertIn('ended needs input', text(section(html, 'How it got here')))

    def test_a_task_without_log_moves_still_names_its_person_wait(self):
        tracker = TODAY_TRACKER.split("## Log")[0]
        for named in (True, False):
            with self.subTest(named=named):
                text = tracker if named else tracker.replace("| Cache warmup |", "| |")
                html = ip.render(111, [(TODAY, text)], bs.EMPTY, NOW, LANES)
                self.assertIn('>NEEDS INPUT</span>', html)

    def test_decision_context_names_a_person_wait_even_without_a_hold_label(self):
        ctx = dp.Context(NOW, tasks=tuple(parse_tracker(TODAY_TRACKER).tasks))
        self.assertEqual(ctx.where('#111'), 'triage · needs input')

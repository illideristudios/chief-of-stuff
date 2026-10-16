"""#210: one visible name for a person wait, including historical launcher outcomes."""

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"), str(Path(__file__).resolve().parent)]
import board_sources as bs
import columns
import decision_page as dp
import flow_chart as fc
import issue_page as ip
import render_board as rb
import settings
import tracker_log as tl
import workers_page as wp
from tracker import parse_tracker
from test_flow_chart import CT, KANBAN, LANES, NOW, TODAY, TODAY_TRACKER
from test_issue_page import page, section, text, today_tracker


class NeedsInputTest(unittest.TestCase):
    def test_a_waiting_card_keeps_its_machine_kind_and_shows_needs_input(self):
        tasks = parse_tracker(TODAY_TRACKER).tasks
        cards = [c for col in rb.build_columns(tasks, LANES) for c in col.cards]
        card = next(c for c in cards if c.name == "Cache warmup")
        self.assertEqual(card.kind, "waiting")
        self.assertEqual(card.state, "needs input")
        self.assertEqual(card.flag, columns.Mark("NEEDS INPUT", "hold"))
        html = columns.render([columns.Column("triage", (card,))])
        self.assertEqual(html.casefold().count('>needs input<'), 1)

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

    def test_a_nameless_task_with_log_moves_still_names_its_person_wait(self):
        tracker = TODAY_TRACKER.replace("| Cache warmup |", "| |")
        html = ip.render(111, [(TODAY, tracker)], bs.EMPTY, NOW, LANES)
        self.assertIn('<span class="chip needs-input">NEEDS INPUT</span>', html)

    def test_a_person_wait_in_any_current_task_sets_the_issue_status(self):
        tracker = TODAY_TRACKER.replace('#111', '#109').replace('#107', '#109')
        html = ip.render(109, [(TODAY, tracker)], bs.EMPTY, NOW, LANES)
        self.assertIn('>NEEDS INPUT</span>', html)

    def test_a_person_wait_does_not_override_a_closed_issue(self):
        sources = replace(bs.EMPTY, issues={'#111': bs.Issue('#111', '', 'Cache', 'closed', (), closed_at=NOW)})
        html = ip.render(111, [(TODAY, TODAY_TRACKER)], sources, NOW, LANES)
        self.assertIn('>CLOSED</span>', html)

    def test_decision_context_names_a_person_wait_even_without_a_hold_label(self):
        ctx = dp.Context(NOW, tasks=tuple(parse_tracker(TODAY_TRACKER).tasks))
        self.assertEqual(ctx.where('#111'), 'triage · needs input')

    def test_a_change_inherits_its_issues_person_wait(self):
        for ref in ('!58', '#58'):
            with self.subTest(ref=ref):
                change = bs.Change(ref, '', 'Cache', 'open', False, None, False, 0, None, 'main', (), ('#111',))
                ctx = dp.Context(NOW, tasks=tuple(parse_tracker(TODAY_TRACKER).tasks),
                                 sources=replace(bs.EMPTY, changes={ref: change}))
                self.assertEqual(ctx.where(ref), 'triage · needs input')

    def test_decision_waits_match_the_issue_cell_and_its_repository(self):
        from backlog_ref import GitHubBacklog
        tasks = tuple(parse_tracker(TODAY_TRACKER).tasks)
        wait = next(t for t in tasks if t.issue == '#111')
        for issue in ('#112', 'other/repo#111', 'https://github.com/other/repo/issues/111'):
            with self.subTest(issue=issue):
                ctx = dp.Context(NOW, tasks=(replace(wait, issue=issue, item='Related to #111'),),
                                 sources=replace(bs.EMPTY, home=GitHubBacklog('o/app')))
                self.assertNotIn('needs input', ctx.where('#111'))

    def test_decision_waits_ignore_standing_placeholders(self):
        wait = next(t for t in parse_tracker(TODAY_TRACKER).tasks if t.issue == '#111')
        standing = replace(wait, owner='impl02', item='impl02: standing implementer. Wait idle')
        self.assertTrue(standing.standing)
        self.assertNotIn('needs input', dp.Context(NOW, tasks=(standing,)).where('#111'))

    def test_decision_waits_respect_terminal_and_effective_stages(self):
        wait = next(t for t in parse_tracker(TODAY_TRACKER).tasks if t.issue == '#111')
        merged = bs.Change('!58', '', 'Cache', 'merged', False, None, False, 0, NOW, 'main', (), ('#111',))
        for task, sources in ((replace(wait, stage='main'), bs.EMPTY),
                              (wait, replace(bs.EMPTY, changes={'!58': merged}))):
            with self.subTest(stage=task.stage, merged=bool(sources.changes)):
                ctx = dp.Context(NOW, tasks=(task,), sources=sources, lanes=LANES)
                self.assertNotIn('needs input', ctx.where('#111'))

    def test_decision_context_loads_lane_and_kanban_holds_from_the_workspace(self):
        from test_decision_page import BLOCK, BOARD
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'CLAUDE.md').write_text(BLOCK.format(board=BOARD))
            (root / 'daily').mkdir()
            (root / 'daily' / f'{TODAY}-tracker.md').write_text(TODAY_TRACKER.replace('| Robin | waiting |', '| unassigned | open |'))
            from unittest.mock import patch
            with patch.object(dp, 'load_settings', return_value=settings.Settings(lanes=LANES, kanban=KANBAN)):
                ctx = dp.decision_context(root, TODAY, NOW)
            self.assertEqual(ctx.where('#111'), 'triage · needs input')

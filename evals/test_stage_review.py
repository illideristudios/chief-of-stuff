"""Completed stage reviews require launcher evidence, independent of entry gates (#130)."""

import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import board_sources
import render_board
import settings
import task_forge
import tracker_log
from tracker import parse_tracker

TRACKER = """## Tasks
| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Busy | Busy | worker | running 09:00 | 09:00 | | M | code | implement | #1 | Busy |
| Failed | Failed | Robin | waiting | 09:00 | | M | code | plan | #2 | Failed |
| Review plan | Review plan | Robin | waiting | 09:00 | | M | code | plan | #3 | Plan |
"""
DAY = date(2000, 1, 1)


def completion(task="Review plan", stage="plan"):
    text = "## Log\n" + tracker_log.started_line("09:00", "w", "claude", "opus", "trees/w", task) + "\n"
    text += tracker_log.ended_line("10:00", "w", "done", "completed", "artifact", review_stage=stage)
    return tracker_log.moves(text, DAY, timezone.utc)


class StageReviewTest(unittest.TestCase):
    def test_completed_and_failed_waiting_tasks_at_a_terminal_review_stage(self):
        tasks = parse_tracker(TRACKER).tasks
        lanes = {"code": settings.Lane(("implement", "plan"))}
        config = settings.Settings()
        held = task_forge.hold_causes(tasks, None, board_sources.EMPTY, lanes, (),
                                      config.review_stages(), moves=completion())
        self.assertEqual(held.get(tasks[2]), task_forge.STAGE_REVIEW)
        self.assertNotEqual(held.get(tasks[1]), task_forge.STAGE_REVIEW)
        cols = render_board.build_columns(tasks, lanes, held=held, review_stages=config.review_stages())
        self.assertEqual(cols[0].name, "plan")
        self.assertEqual(cols[0].note, task_forge.REVIEW_LABEL)
        self.assertEqual([c.name for c in cols[0].cards], ["Review plan", "Failed"])
        self.assertEqual(cols[0].cards[0].flag.text, task_forge.REVIEW_LABEL)
        began = task_forge.hold_began(held, completion(), {}, None, tasks)
        self.assertEqual(began[tasks[2]], datetime(2000, 1, 1, 10, tzinfo=timezone.utc))
        self.assertEqual(task_forge.went_held(held, began, datetime(2000, 1, 1, 9, tzinfo=timezone.utc)), 1)

    def test_disabled_review_and_no_implicit_plan_default(self):
        tasks = parse_tracker(TRACKER).tasks
        lanes = {"code": settings.Lane(("implement", "plan", "merge"))}
        config = settings.Settings(stages={"plan": settings.Stage(review_after=False)})
        for reviews in (config.review_stages(), ()):
            held = task_forge.hold_causes(tasks, None, board_sources.EMPTY, lanes, (), reviews, moves=completion())
            self.assertNotIn(task_forge.STAGE_REVIEW, held.values())
        held = task_forge.hold_causes(tasks, None, board_sources.EMPTY, lanes, ())
        self.assertNotIn(task_forge.STAGE_REVIEW, held.values())

    def test_pending_decision_and_forge_merge_take_precedence(self):
        task = parse_tracker(TRACKER).tasks[2]
        lanes = {"code": settings.Lane(("implement", "plan", "merge"))}
        held = task_forge.hold_causes([task], None, board_sources.EMPTY, lanes, {"#3"}, {"plan"}, moves=completion())
        self.assertEqual(held[task], "decision")
        self.assertEqual(task_forge.hold_cause(task, None, "merge", workers=set(), pending_refs=(), home=None,
                         lane=lanes["code"], review_stages={"merge"}, completed_review=True), "")

    def test_later_attempt_invalidates_completion_and_worker_reason_cannot_forge_it(self):
        tasks = parse_tracker(TRACKER).tasks
        later = tracker_log.moves("## Log\n" + tracker_log.started_line("11:00", "w2", "claude", "opus", "trees/w", "Review plan"), DAY, timezone.utc)
        held = task_forge.hold_causes(tasks, None, board_sources.EMPTY, {}, (), {"plan"}, moves=completion() + later)
        self.assertNotEqual(held.get(tasks[2]), task_forge.STAGE_REVIEW)
        fake = "## Log\n" + tracker_log.started_line("09:00", "w", "claude", "opus", "trees/w", "Review plan") + "\n"
        fake += tracker_log.ended_line("10:00", "w", "human_review", "[review_after=plan] plan needs your review", "none")
        held = task_forge.hold_causes(tasks, None, board_sources.EMPTY, {}, (), {"plan"}, moves=tracker_log.moves(fake, DAY, timezone.utc))
        self.assertNotEqual(held.get(tasks[2]), task_forge.STAGE_REVIEW)

    def test_pr_review_stage_keeps_its_own_column(self):
        task = replace(parse_tracker(TRACKER).tasks[2], stage="pr")
        lanes = {"code": settings.Lane(("implement", "pr", "review", "merge"))}
        cols = render_board.build_columns([task], lanes, held={task: task_forge.STAGE_REVIEW}, review_stages={"pr"})
        self.assertEqual(cols[0].name, "pr")
        self.assertEqual(cols[0].note, task_forge.REVIEW_LABEL)
        cols = render_board.build_columns([task], lanes, held={task: "decision"})
        self.assertTrue(any(c.name == "pr" and c.cards for c in cols))

    def test_board_and_issue_page_read_completion_evidence(self):
        import issue_page
        from workspace import Config
        moves = completion()
        text = TRACKER + "## Log\n" + "\n".join(m.line for m in moves) + "\n"
        cfg = Config("", "", "", "UTC", (), None, None)
        now = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)
        lanes = {"code": settings.Lane(("implement", "plan", "merge"))}
        board = render_board.render(text, cfg, now, lanes, review_stages=settings.Settings().review_stages())
        self.assertIn(task_forge.REVIEW_LABEL, board)
        page = issue_page.render(3, [(DAY, text)], board_sources.EMPTY, now, lanes, cfg,
                                 review_stages=settings.Settings().review_stages())
        self.assertIn(task_forge.REVIEW_LABEL, page)

    def test_a_terminal_stage_stays_open_only_for_a_confirmed_review(self):
        task = replace(parse_tracker(TRACKER).tasks[2], stage="merge")
        lanes = {"code": settings.Lane(("implement", "plan", "merge"))}
        bare = task_forge.hold_causes([task], None, board_sources.EMPTY, lanes, {"#3"}, {"merge"})
        self.assertNotIn(task, bare)
        held = task_forge.hold_causes([task], None, board_sources.EMPTY, lanes, (), {"merge"},
                                      moves=completion(stage="merge"))
        self.assertEqual(held[task], task_forge.STAGE_REVIEW)

    def test_an_item_completion_and_a_renamed_row_are_one_task(self):
        task = replace(parse_tracker(TRACKER).tasks[2], item="Cache design")
        earlier = replace(task, name="Old plan", item="Old plan")
        lanes = {"code": settings.Lane(("implement", "plan", "merge"))}
        old = completion("Old plan")
        held = task_forge.hold_causes([task], None, board_sources.EMPTY, lanes, (), {"plan"},
                                      moves=old, earlier=[earlier])
        self.assertEqual(held[task], task_forge.STAGE_REVIEW)
        later = tracker_log.moves("## Log\n" + tracker_log.stage_line("11:00", "Review plan", "implement"), DAY, timezone.utc)
        held = task_forge.hold_causes([task], None, board_sources.EMPTY, lanes, (), {"plan"},
                                      moves=old + later, earlier=[earlier])
        self.assertNotEqual(held.get(task), task_forge.STAGE_REVIEW)
        named = replace(parse_tracker(TRACKER).tasks[2], item="Cache design")
        by_item = completion("Cache design")
        held = task_forge.hold_causes([named], None, board_sources.EMPTY, lanes, (), {"plan"}, moves=by_item)
        self.assertEqual(held[named], task_forge.STAGE_REVIEW)
        moved = tracker_log.moves("## Log\n" + tracker_log.stage_line("11:00", "Review plan", "implement"), DAY, timezone.utc)
        held = task_forge.hold_causes([named], None, board_sources.EMPTY, lanes, (), {"plan"}, moves=by_item + moved)
        self.assertNotEqual(held.get(named), task_forge.STAGE_REVIEW)

    def test_review_evidence_outside_the_chart_window_and_across_midnight_still_holds(self):
        import flow_chart
        import issue_page
        from workspace import Config
        lanes = {"code": settings.Lane(("plan",))}
        now = datetime(2000, 1, 8, 12, tzinfo=timezone.utc)
        today = TRACKER
        started = tracker_log.started_line("23:00", "w", "claude", "opus", "trees/w", "Review plan")
        ended = tracker_log.ended_line("00:10", "w", "done", "completed", "artifact", review_stage="plan")
        review_log = [(date(2000, 1, 1), TRACKER + "## Log\n" + started + "\n"),
                      (date(2000, 1, 2), "## Log\n" + ended + "\n")]
        cfg = Config("", "", "", "UTC", (), None, None)
        board = render_board.render(today, cfg, now, lanes, tracker_day=now.date(),
                                    review_log=review_log, review_stages={"plan"})
        self.assertIn(task_forge.REVIEW_LABEL, board)
        page = issue_page.render(3, [(now.date(), today)], board_sources.EMPTY, now, lanes, cfg,
                                 review_stages={"plan"}, review_log=review_log)
        self.assertIn(task_forge.REVIEW_LABEL, page)
        log = tracker_log.day_moves(review_log, timezone.utc)
        self.assertEqual(log[-1].stage, "plan")
        built = flow_chart.build(log, parse_tracker(today).tasks, lanes,
                                 { "Review plan": task_forge.REVIEW_LABEL }, {}, now)
        self.assertEqual(built[0][0], "needs input")

    def test_the_golden_fixture_carries_the_launcher_review_contract(self):
        root = Path(__file__).resolve().parents[0] / "cases/completed-stage-needs-human-review/fixture"
        self.assertIn("[review_after=specify]", (root / "daily/{{today}}-tracker.md").read_text())
        self.assertIn("review_stage: specify", (root / ".chief-of-stuff/reports/cache-design.toon").read_text())

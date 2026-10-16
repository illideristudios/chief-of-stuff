"""Completed stage reviews are holds, independent of entry gates (#130)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import render_board
import settings
import task_forge
from tracker import parse_tracker


TRACKER = """## Tasks
| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Busy | Busy | worker | running 09:00 | 09:00 | | M | code | implement | #1 | Busy |
| Pending | Pending | Robin | waiting | 09:00 | | M | code | implement | #2 | Pending |
| Review plan | Review plan | Robin | waiting | 09:00 | | M | code | plan | #3 | Plan |
"""


class StageReviewTest(unittest.TestCase):
    def test_board_names_reviews_on_card_and_column_and_puts_them_first(self):
        tasks = parse_tracker(TRACKER).tasks
        lanes = {"code": settings.Lane(("implement", "plan", "merge"))}
        cols = render_board.build_columns(tasks, lanes)
        self.assertEqual(cols[0].name, "plan")
        self.assertEqual(cols[0].note, "needs your review")
        self.assertEqual(cols[0].cards[0].flag.text, "needs your review")
        held = task_forge.hold_causes(tasks, None, render_board.board_sources.EMPTY, lanes, frozenset())
        self.assertEqual(held[tasks[2]], "needs your review")

    def test_running_marked_stage_is_not_held_and_custom_stages_are_supported(self):
        tasks = parse_tracker(TRACKER).tasks
        lanes = {"code": settings.Lane(("implement", "plan", "merge"))}
        cols = render_board.build_columns(tasks, lanes, review_stages=frozenset({"implement"}))
        self.assertEqual(cols[0].name, "implement")
        self.assertEqual([c.name for c in cols[0].cards], ["Pending", "Busy"])
        self.assertIsNone(cols[0].cards[1].flag)
        self.assertEqual(cols[0].cards[0].flag.text, "needs your review")


if __name__ == "__main__":
    unittest.main()

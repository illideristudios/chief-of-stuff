"""Unit tests for scripts/orphans.py: whose is a tree at risk, and are they still here (#43).

Pure. The claims arrive with liveness already decided by the sources that know it; this is only the join.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from orphans import Claim, Orphan, Unclaimed, judge  # noqa: E402
from tree_state import TreeState  # noqa: E402

AT_RISK = TreeState(branch="feat/x", dirty=0, off_origin=1, unpushed=None)
SAFE = TreeState(branch="feat/x", dirty=0, off_origin=0, unpushed=None)
GONE = Claim(owner="sam", live=False, absence="is not in ## Sessions")
LIVE = Claim(owner="kim", live=True)


class JudgeTest(unittest.TestCase):
    def test_a_tree_with_nothing_at_risk_is_nobody_s_concern(self) -> None:
        self.assertIsNone(judge("wt-x", SAFE, [GONE]))
        self.assertIsNone(judge("wt-x", SAFE, []))

    def test_at_risk_work_whose_only_owner_is_gone_is_orphaned(self) -> None:
        found = judge("wt-x", AT_RISK, [GONE])
        self.assertIsInstance(found, Orphan)
        self.assertEqual(str(found), "orphaned work: wt-x (feat/x) — 1 not on origin/main, no upstream, "
                                     "and its owner 'sam' is not in ## Sessions")

    def test_any_live_claim_holds_the_tree(self) -> None:
        """A false orphan sends the user after a session that is working; any sign of life wins."""
        self.assertIsNone(judge("wt-x", AT_RISK, [GONE, LIVE]))

    def test_the_ref_is_named_when_the_absence_turned_on_it(self) -> None:
        found = judge("wt-x", AT_RISK, [Claim("sam", False, "names a ref no ## Sessions row carries", "999999")])
        self.assertIn("'sam' [999999] names a ref", str(found))

    def test_at_risk_work_nothing_claims_is_unclaimed_not_orphaned(self) -> None:
        """Owner gone and owner unknown are different facts, and the tree may be the user's own."""
        found = judge("wt-x", AT_RISK, [])
        self.assertIsInstance(found, Unclaimed)
        self.assertEqual(str(found), "unclaimed work: wt-x (feat/x) — 1 not on origin/main, no upstream; "
                                     "no started line, worker registration or one-shot names this tree")


if __name__ == "__main__":
    unittest.main()

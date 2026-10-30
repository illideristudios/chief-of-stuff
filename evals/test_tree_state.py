"""Unit tests for scripts/tree_state.py: what a worktree holds that exists nowhere else (#43).

No git here. `TreeState` is the four numbers a reader takes from git; `at_risk` is the rule over them.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tree_state  # noqa: E402
from tree_state import TreeState, at_risk, unsaved  # noqa: E402


def state(dirty: int | None = 0, off_origin: int | None = 0, unpushed: int | None = None) -> TreeState:
    return TreeState(branch="feat/x", dirty=dirty, off_origin=off_origin, unpushed=unpushed)


class UnsavedTest(unittest.TestCase):
    def test_ignored_engine_metadata_and_build_output_are_not_unsaved(self) -> None:
        self.assertEqual(unsaved("!! .chief-of-stuff/\n!! node_modules/\n!! scripts/__pycache__/\n"), ())

    def test_only_the_ignored_root_metadata_directory_is_exempt(self) -> None:
        status = "?? .chief-of-stuff/\n!! nested/.chief-of-stuff/\n!! .chief-of-stuff\nM .chief-of-stuff/dispatch.md\n"
        self.assertEqual(unsaved(status), (".chief-of-stuff/", "nested/.chief-of-stuff/", ".chief-of-stuff",
                                           ".chief-of-stuff/dispatch.md"))

    def test_cursor_channel_only_accepts_only_the_single_ignored_root_channel_file(self) -> None:
        self.assertEqual(tree_state.CURSOR_CHANNEL_EXCLUDE, "/.cursor/mcp.json")
        self.assertTrue(tree_state.cursor_channel_only("!! .cursor/mcp.json"))
        self.assertTrue(tree_state.cursor_channel_only("!! .cursor/mcp.json\n"))
        for other in ("", "?? .cursor/mcp.json", "!! .cursor/", "!! .cursor/mcp.json\n!! .cursor/notes.md"):
            with self.subTest(other=other):
                self.assertFalse(tree_state.cursor_channel_only(other))


class AtRiskTest(unittest.TestCase):
    def test_a_clean_tree_on_origin_main_holds_nothing(self) -> None:
        self.assertEqual(at_risk(state()), ())

    def test_uncommitted_files_are_counted(self) -> None:
        self.assertEqual(at_risk(state(dirty=2)), ("2 uncommitted",))

    def test_a_commit_with_no_upstream_exists_only_here(self) -> None:
        """The field report: one local commit, never pushed, and the audit printed nothing."""
        self.assertEqual(at_risk(state(off_origin=1)), ("1 not on origin/main", "no upstream"))

    def test_a_commit_ahead_of_its_upstream_is_unpushed(self) -> None:
        self.assertEqual(at_risk(state(off_origin=3, unpushed=1)), ("3 not on origin/main", "1 unpushed"))

    def test_a_branch_pushed_to_its_upstream_is_safe_off_main(self) -> None:
        """Unmerged is not at risk once a remote holds it: it is recoverable by name from there."""
        self.assertEqual(at_risk(state(off_origin=3, unpushed=0)), ())

    def test_a_branch_at_main_needs_no_upstream(self) -> None:
        self.assertEqual(at_risk(state(off_origin=0, unpushed=None)), ())

    def test_an_unknown_origin_is_never_read_as_safe(self) -> None:
        self.assertEqual(at_risk(state(off_origin=None)), ("origin/main unknown", "no upstream"))

    def test_but_a_pushed_branch_is_safe_whatever_origin_main_says(self) -> None:
        self.assertEqual(at_risk(state(off_origin=None, unpushed=0)), ())

    def test_dirt_and_commits_are_both_named(self) -> None:
        self.assertEqual(at_risk(state(dirty=1, off_origin=1)), ("1 uncommitted", "1 not on origin/main", "no upstream"))

    # #443 slice 3 review R8: `dirty` is None when git could not say, as `off_origin` already is; unknown is one fact, said once.
    def test_an_unknown_dirty_count_says_uncommitted_work_is_unknown_and_nothing_false(self) -> None:
        found = at_risk(TreeState("b", None, None, None))
        self.assertNotIn("no upstream", found, "the status was unreadable; nothing was learned about an upstream")
        self.assertFalse([x for x in found if "0 uncommitted" in x], found)
        self.assertIn(tree_state.UNCOMMITTED_UNKNOWN, found)

    def test_unknown_uncommitted_work_is_named_even_when_every_commit_is_safe(self) -> None:
        """A gitlink tree on origin/main (or pushed): history is fine, the status is not, and the work is still at risk."""
        for off_origin, unpushed in ((0, None), (None, 0), (0, 0)):
            with self.subTest(off_origin=off_origin, unpushed=unpushed):
                self.assertEqual(at_risk(TreeState("b", None, off_origin, unpushed)), (tree_state.UNCOMMITTED_UNKNOWN,))

    def test_the_phrase_is_one_string_whatever_the_history_says(self) -> None:
        self.assertEqual(tree_state.UNCOMMITTED_UNKNOWN, "uncommitted work unknown (status unreadable)")
        self.assertEqual(at_risk(TreeState("b", None, 2, 0)), (tree_state.UNCOMMITTED_UNKNOWN,))
        self.assertEqual(at_risk(TreeState("b", None, 2, None))[0], tree_state.UNCOMMITTED_UNKNOWN)


if __name__ == "__main__":
    unittest.main()

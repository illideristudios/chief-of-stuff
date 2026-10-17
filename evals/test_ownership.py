"""#57: one rule for a read-only task, read the same way by the prompt and the launcher."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ownership  # noqa: E402


class IssueScopesTest(unittest.TestCase):
    def test_issue_section_uses_the_shared_table_and_bullet_reader(self):
        body = "## File ownership\n| Mode | Paths |\n| --- | --- |\n| write | `src/`, `tests/` |\n- read-only: `docs/`\n## Other\n- write: `secrets/`"
        self.assertEqual(ownership.issue_scopes(body), [("src", "write"), ("tests", "write"), ("docs", "read")])

    def test_absent_empty_or_invalid_scope_defaults_to_whole_repository_write(self):
        for body in ("", "## File ownership\n", "## File ownership\n- typo: `src/`",
                     "## File ownership\n- write: `../outside`", "## File ownership\n- write: `/absolute`"):
            with self.subTest(body=body):
                self.assertEqual(ownership.issue_scopes(body), [(".", "write")])


class ReadOnlyTest(unittest.TestCase):
    def test_a_cell_that_starts_with_the_word_none_is_read_only(self):
        for cell in ("none", "None; read-only review", "  NONE; read-only", "none; worktree `t` (b)",
                     "none (read-only)", "none, review only"):
            with self.subTest(cell=cell):
                self.assertIs(ownership.read_only(cell), True)

    def test_a_cell_that_names_a_path_is_not_read_only_however_it_starts(self):
        for cell in ("none.py", "None/x.py", "none-such/ and src/a/", "nonexistent/dir/",
                     "src/a/; none of it generated", ""):
            with self.subTest(cell=cell):
                self.assertIs(ownership.read_only(cell), False)


class OverlapTest(unittest.TestCase):
    """#365 review (R10): `ownership.overlaps(a, b)` is the one definition of "overlap", read by the launcher and `worker --check`.

    Two File ownership cells overlap when any path of one equals, contains or is contained in a path of the other. A path is a
    backticked token in the cell (a cell with none is split on commas), compared on path-segment boundaries, ignoring a
    leading `./` and a trailing `/`. The `; worktree ...` suffix the launcher adds is no part of the cell. A read-only cell
    (`ownership.read_only`) and an empty cell overlap nothing. Not defined here, filed: glob characters in a path.
    """

    def test_equal_contained_and_containing_paths_overlap_in_both_directions(self):
        for a, b in (("`src/a/upload.py`", "`src/a/upload.py`"),
                     ("`src/a/`", "`src/a/upload.py`"),
                     ("`src/a`", "`src/a/deep/er/x.py`"),
                     ("`src/a/`", "`src/a`"),
                     ("`./src/a/`", "`src/a/x.py`"),
                     ("`notes/x.md`, `src/a/`", "`src/b/`, `src/a/upload.py`")):
            for first, second in ((a, b), (b, a)):
                with self.subTest(first=first, second=second):
                    self.assertIs(ownership.overlaps(first, second), True)

    def test_a_name_that_only_starts_the_same_does_not_overlap(self):
        for a, b in (("`src/a`", "`src/ab`"), ("`src/a/`", "`src/ab/x.py`"), ("`src/a/upload.py`", "`src/a/upload.pyc`"),
                     ("`src/a/`", "`src/b/`"), ("`tests/a/`", "`src/a/`")):
            for first, second in ((a, b), (b, a)):
                with self.subTest(first=first, second=second):
                    self.assertIs(ownership.overlaps(first, second), False)

    def test_prose_around_the_paths_is_ignored_but_a_backticked_path_in_it_counts(self):
        row = "`src/a/` (the upload handler; tests under `tests/a/`)"
        self.assertIs(ownership.overlaps(row, "`tests/a/test_upload.py`"), True)
        self.assertIs(ownership.overlaps(row, "`docs/`"), False)

    def test_a_cell_with_no_backticks_is_read_as_comma_separated_paths(self):
        self.assertIs(ownership.overlaps("src/a/, notes/x.md", "`src/a/upload.py`"), True)
        self.assertIs(ownership.overlaps("src/a/, notes/x.md", "`src/b/`"), False)

    def test_the_worktree_suffix_the_launcher_adds_is_ignored(self):
        started = "`src/a/`; worktree `trees/src/a-fix` (feat/a)"
        self.assertIs(ownership.overlaps(started, "`trees/src/a-fix/`"), False)
        self.assertIs(ownership.overlaps(started, "`src/a/upload.py`"), True)
        self.assertIs(ownership.overlaps("`src/b/`", started), False)

    def test_a_read_only_cell_and_an_empty_cell_overlap_nothing(self):
        for quiet in ("none", "None; read-only review of `src/a/`", "none (read-only)", "none; worktree `t` (b)", "", "   "):
            with self.subTest(quiet=quiet):
                self.assertIs(ownership.overlaps(quiet, "`src/a/`"), False)
                self.assertIs(ownership.overlaps("`src/a/`", quiet), False)
                self.assertIs(ownership.overlaps(quiet, quiet), False)

    def test_a_cell_that_owns_the_whole_repo_overlaps_every_other_cell_that_owns_anything(self):
        # #365 verify R32: `.` and `./` name the repository root, which contains every path; read today as no path at all, so two tasks
        # could share a tree. Both directions, with the launcher's suffix and prose as for any cell.
        for whole in ("`.`", "`./`", ".", "./", "`./` (the whole repository)", "`src/b/`, `.`", "`.`; worktree `trees/all` (feat/all)"):
            for other in ("`src/a/`", "`src/a/upload.py`", "src/a/", "`notes/x.md`, `src/b/`", "`.`", "`./`"):
                for first, second in ((whole, other), (other, whole)):
                    with self.subTest(first=first, second=second):
                        self.assertIs(ownership.overlaps(first, second), True)

    def test_a_whole_repo_cell_still_overlaps_nothing_that_owns_nothing(self):
        for quiet in ("none", "None; read-only review of `src/a/`", "", "   "):
            for whole in ("`.`", "`./`"):
                with self.subTest(quiet=quiet, whole=whole):
                    self.assertIs(ownership.overlaps(whole, quiet), False)
                    self.assertIs(ownership.overlaps(quiet, whole), False)

    def test_a_cell_overlaps_a_copy_of_itself(self):
        # The launcher excludes the row being launched by key, not by this test: a task is never compared with itself.
        self.assertIs(ownership.overlaps("`src/a/`", "`src/a/`"), True)


if __name__ == "__main__":
    unittest.main()

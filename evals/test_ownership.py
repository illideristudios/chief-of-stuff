"""#57: one rule for a read-only task, read the same way by the prompt and the launcher."""

import re
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ownership  # noqa: E402


class IssueScopesTest(unittest.TestCase):
    def test_issue_section_uses_the_shared_table_and_bullet_reader(self):
        body = "## File ownership\n| Mode | Paths |\n| --- | --- |\n| write | `src/`, `tests/` |\n- read-only: `docs/`\n## Other\n- write: `secrets/`"
        self.assertEqual(ownership.issue_scopes(body), [("src", "write"), ("tests", "write"), ("docs", "read")])

    def test_absent_empty_or_invalid_scope_has_no_scope(self):
        for body in ("", "## File ownership\n", "## File ownership\n- typo: `src/`",
                     "## File ownership\n- write: `../outside`", "## File ownership\n- write: `/absolute`"):
            with self.subTest(body=body):
                self.assertIsNone(ownership.issue_scopes(body))

    def test_a_malformed_line_invalidates_the_declaration(self):
        for bad in ("write src/", "- write src/", "| write |", "some prose", "| write | src/ | extra |", "| Mode | src/ |", "- mode: src/"):
            with self.subTest(bad=bad):
                self.assertIsNone(ownership.issue_scopes("## File ownership\n- read-only: docs/\n" + bad))

    def test_noncanonical_path_segments_cannot_bypass_overlap(self):
        for path in ("src//a.py", "src/./a.py", "src/../a.py", "./", ".//", "./."):
            with self.subTest(path=path):
                self.assertIsNone(ownership.issue_scopes("## File ownership\n- read-only: docs/\n- write: " + path))

    def test_cells_that_cannot_round_trip_through_overlap_have_no_scope(self):
        for paths in ("`none`", "`none, src`", "`src,a.py`", "`src/a; worktree hidden`",
                      "`src/a` src/b", "src/a, `src/b`", "`src/a` and `src/b`", "`src/a` `src/b`",
                      "`src/a", "`src/a` trailing", "` src/a `"):
            with self.subTest(paths=paths):
                scopes = ownership.issue_scopes("## File ownership\n- read-only: docs/\n- write: " + paths)
                self.assertIsNone(scopes)

    def test_whole_repository_scope_is_explicit_and_read_only_stays_distinct(self):
        for mode, expected in (("write", "write"), ("read-only", "read")):
            with self.subTest(mode=mode):
                self.assertEqual(ownership.issue_scopes(f"## File ownership\n- {mode}: `.`"), [(".", expected)])

    def test_empty_path_rows_are_ignored_only_when_a_valid_path_remains(self):
        for empty in ("- write:", "- read-only:", "| write | |"):
            with self.subTest(empty=empty):
                self.assertIsNone(ownership.issue_scopes("## File ownership\n" + empty))
                for paths, expected in (("- write: `src/`", [("src", "write")]),
                                        ("- read-only: `docs/`", [("docs", "read")])):
                    self.assertEqual(ownership.issue_scopes("## File ownership\n" + empty + "\n" + paths), expected)
        self.assertIsNone(ownership.issue_scopes("## File ownership\n- typo:\n- write: `src/`"))
        self.assertIsNone(ownership.issue_scopes("## File ownership\n- write: none\n- read-only: docs/"))

    def test_issue_forms_require_input_and_submit_a_parseable_section(self):
        templates = Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE"
        for name in ("bug.yml", "task.yml"):
            with self.subTest(template=name):
                # Read the actual ownership field and its literal YAML block without a YAML dependency.
                fields = (templates / name).read_text().split("  - type: ")[1:]
                ownership_fields = [field for field in fields if "    id: file_ownership\n" in field]
                self.assertEqual(len(ownership_fields), 1, "the form must include the ownership field")
                field = ownership_fields[0]
                self.assertRegex(field, r"validations:\n      required: true")
                default = re.search(r"(?m)^      value:", field)
                self.assertIsNone(default, "a prefilled skeleton satisfies required without declaring a path")
                placeholder = re.search(r"(?m)^      placeholder: \|\n((?:        .*\n)+)", field)
                self.assertIsNotNone(placeholder)
                skeleton = textwrap.dedent(placeholder[1]).rstrip()
                for submitted in (skeleton.replace("- write:", "- write: `src/`"),
                                  skeleton + "\n- write: `src/`"):
                    # GitHub renders each form field's label and response in template order.
                    body = "\n\n".join(
                        f"### {re.search(r'(?m)^      label: (.+)$', entry)[1]}\n\n"
                        + (submitted if entry == field else "Example response") for entry in fields)
                    self.assertEqual(ownership.issue_scopes(body), [("src", "write")])


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


class ScopeConsultContextTest(unittest.TestCase):
    def test_context_includes_paths_issue_title_body_and_plan_text(self):
        scopes = [("scripts/a.py", "write"), ("docs/a.md", "read")]
        first = ownership.scope_consult_context(
            title="Widget crash on empty input",
            body="Acceptance: handle empty input without raising.",
            plan_text="# Plan\n Guard empty input in scripts/a.py.",
            scopes=scopes,
        )
        second = ownership.scope_consult_context(
            title="Widget crash on empty input",
            body="Acceptance: handle empty input without raising.",
            plan_text="# Plan\n Guard empty input in scripts/a.py.",
            scopes=scopes,
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("Proposed file ownership: write: scripts/a.py; read: docs/a.md"))
        self.assertIn("Issue title: Widget crash on empty input", first)
        self.assertIn("Issue body:\nAcceptance: handle empty input without raising.", first)
        self.assertIn("Plan:\n# Plan\n Guard empty input in scripts/a.py.", first)

    def test_empty_title_body_and_missing_plan_use_explicit_placeholders(self):
        ctx = ownership.scope_consult_context(
            title="   ",
            body="",
            plan_text="",
            scopes=[("scripts/a.py", "write")],
        )
        self.assertIn("Proposed file ownership: write: scripts/a.py", ctx)
        self.assertIn("Issue title: (empty)", ctx)
        self.assertIn("Issue body:\n(empty)", ctx)
        self.assertIn("Plan: (not found in worktree)", ctx)

    def test_oversized_plan_and_body_truncate_in_order_within_cap_while_preserving_paths(self):
        scopes = [("scripts/a.py", "write"), ("docs/a.md", "read")]
        huge_plan = "P" * (ownership.SCOPE_CONTEXT_CAP + 4000)
        ctx = ownership.scope_consult_context(
            title="Keep title intact",
            body="Keep issue body intact.",
            plan_text=huge_plan,
            scopes=scopes,
        )
        self.assertLessEqual(len(ctx.encode("utf-8")), ownership.SCOPE_CONTEXT_CAP)
        self.assertTrue(ctx.startswith("Proposed file ownership: write: scripts/a.py; read: docs/a.md"))
        self.assertIn("Issue title: Keep title intact", ctx)
        self.assertIn("Issue body:\nKeep issue body intact.", ctx)
        self.assertIn("[plan truncated]", ctx)
        self.assertNotIn("[issue body truncated]", ctx)

        huge_body = "B" * (ownership.SCOPE_CONTEXT_CAP + 4000)
        ctx_both = ownership.scope_consult_context(
            title="Keep title intact",
            body=huge_body,
            plan_text="Plan details " * 200,
            scopes=scopes,
        )
        self.assertLessEqual(len(ctx_both.encode("utf-8")), ownership.SCOPE_CONTEXT_CAP)
        self.assertTrue(ctx_both.startswith("Proposed file ownership: write: scripts/a.py; read: docs/a.md"))
        self.assertIn("Issue title: Keep title intact", ctx_both)
        self.assertIn("[plan truncated]", ctx_both)
        self.assertIn("[issue body truncated]", ctx_both)

        huge_title = "T" * (ownership.SCOPE_CONTEXT_CAP + 4000)
        ctx_title = ownership.scope_consult_context(
            title=huge_title,
            body="Body text",
            plan_text="Plan text",
            scopes=scopes,
        )
        self.assertLessEqual(len(ctx_title.encode("utf-8")), ownership.SCOPE_CONTEXT_CAP)
        self.assertTrue(ctx_title.startswith("Proposed file ownership: write: scripts/a.py; read: docs/a.md"))
        self.assertIn("[title truncated]", ctx_title)

        multibyte_plan = "é🔧" * (ownership.SCOPE_CONTEXT_CAP // 2)
        ctx_utf8 = ownership.scope_consult_context(
            title="UTF-8 boundary",
            body="Body text",
            plan_text=multibyte_plan,
            scopes=scopes,
        )
        self.assertLessEqual(len(ctx_utf8.encode("utf-8")), ownership.SCOPE_CONTEXT_CAP)
        self.assertIn("[plan truncated]", ctx_utf8)


if __name__ == "__main__":
    unittest.main()

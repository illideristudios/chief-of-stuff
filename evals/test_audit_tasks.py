"""Unit tests for scripts/audit_tasks.py: a task is done when its change is on main, and a script says so, not a grep.

Repos here are real: a bare origin, a clone that has fetched it, and worktrees off that clone.
`git merge-base --is-ancestor` and `git status --porcelain` have no useful fake, and the point of the
script is that its answer is git's answer.
"""

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_tasks as al  # noqa: E402
import git_trees  # noqa: E402
import git_view  # noqa: E402
import tracker_log as tl  # noqa: E402
import board_sources as bs  # noqa: E402
import test_git_trees as tgt  # noqa: E402  (module-qualified: don't re-collect its TestCases)
import test_board_sources as tbs  # noqa: E402  (module-qualified: don't re-collect its TestCases)
from tracker import Session, Task, short_name  # noqa: E402
import tree_state  # noqa: E402
from evals import git_taint as taint  # noqa: E402

_views = contextlib.ExitStack()


def setUpModule() -> None:
    """Reads go through git_view.run (#443): give them a private base, never the user's cache."""
    _views.enter_context(taint.private_git_views(Path(_views.enter_context(tempfile.TemporaryDirectory()))))


def tearDownModule() -> None:
    _views.close()


CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Worktrees: `trees/`
- Timezone: America/Chicago
"""

TRACKER = """# Tracker 2026-09-17

Coordinator: coordinator. Board: none.

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
{rows}

## Decisions

| time | item | Robin's words |
|---|---|---|

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
{sessions}

## Log

- 09:00 opened the day
{log}
"""


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd), "GIT_TERMINAL_PROMPT": "0"},
    )
    return out.stdout.strip()


def build(root: Path) -> dict[str, Path]:
    """A bare origin, a clone with origin/main fetched, and three worktrees: merged, unmerged, dirty."""
    origin = root / "origin.git"
    origin.mkdir()
    git("init", "--bare", "--initial-branch=main", ".", cwd=origin)

    clone = root / "repo"
    clone.mkdir()
    git("init", "--initial-branch=main", ".", cwd=clone)
    git("config", "user.email", "t@example.test", cwd=clone)
    git("config", "user.name", "Test", cwd=clone)
    (clone / "README.md").write_text("base\n")
    git("add", "README.md", cwd=clone)
    git("commit", "-m", "base", cwd=clone)
    git("remote", "add", "origin", str(origin), cwd=clone)
    git("push", "-u", "origin", "main", cwd=clone)

    trees = root / "trees"
    trees.mkdir()

    # A branch whose commit is already on main: work that is genuinely finished.
    git("branch", "landed", cwd=clone)
    git("worktree", "add", str(trees / "wt-merged"), "landed", cwd=clone)

    # A branch with a commit of its own, never merged.
    git("worktree", "add", "-b", "feat/open", str(trees / "wt-unmerged"), "main", cwd=clone)
    (trees / "wt-unmerged" / "new.txt").write_text("work\n")
    git("add", "new.txt", cwd=trees / "wt-unmerged")
    git("commit", "-m", "work", cwd=trees / "wt-unmerged")

    # A branch on main, but with the work still uncommitted.
    git("worktree", "add", "-b", "feat/dirty", str(trees / "wt-dirty"), "main", cwd=clone)
    (trees / "wt-dirty" / "scratch.txt").write_text("unsaved\n")

    return {"origin": origin, "clone": clone, "trees": trees}


def session_row(name: str, ref: str = "a1b2c3") -> str:
    return f"| {ref} | {name} | working | a task | | now | | none | 09:30 |"


def started(task: str, tree: str) -> str:
    """The Log's record of a one-shot launched into `tree` for `task`: the tracker's only claim on a tree (#1039)."""
    return tl.started_line("09:05", "worker01", "codex", "", f"worktree `{tree}`", task)


def workspace(rows: str, log: str, sessions: str = "") -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "daily").mkdir()
    (root / "CLAUDE.md").write_text(CLAUDE)
    (root / "daily" / "2026-09-17-tracker.md").write_text(
        TRACKER.format(rows=rows, sessions=sessions, log=log))
    build(root)
    return tmp, root


def refuse(tree: Path, how: str = "symlink") -> None:
    """Make the view refuse `tree` (#443): a worker-made `.git` that is a symlink, or one far past the size bound. Every read
    of it is then `(128, "unreadable: ...")`, with a different reason for each `how`."""
    dotgit = tree / ".git"
    content = dotgit.read_bytes()
    dotgit.unlink()
    if how == "symlink":
        target = tree.parent.parent / f"{tree.name}.gitfile"
        target.write_bytes(content)
        dotgit.symlink_to(target)
    else:
        dotgit.write_bytes(content + b"\n" * 4096)
    assert git_trees.git(["rev-parse", "HEAD"], tree)[0] == 128


def refusing(*words: str):
    """`git_trees.git` as it is, except a call whose arguments start with `words` is refused: git could not say (128), which
    is not git's real `no` (1). The rest of the tree reads as it did."""
    real = git_trees.git

    def fake(args, cwd, *rest, **kw):
        return (128, "unreadable: refused") if list(args[:len(words)]) == list(words) else real(args, cwd, *rest, **kw)

    return patch.object(git_trees, "git", fake)


def unreadable_phrase() -> str:
    """R7: the one wording for "git could not say", defined once by the code (it must contain `unknown`), in audit_tasks
    or tree_state."""
    for module in (al, tree_state):
        if hasattr(module, "UNREADABLE_PHRASE"):
            return module.UNREADABLE_PHRASE
    raise AssertionError("define UNREADABLE_PHRASE in scripts/audit_tasks.py or scripts/tree_state.py")


class ConfigTest(unittest.TestCase):
    def test_worktrees_line_read_from_the_coordinator_block(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "CLAUDE.md"
            p.write_text(CLAUDE)
            self.assertEqual(al.worktrees_dir(p.read_text()), "trees/")

    def test_no_worktrees_line_means_the_root_itself(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "CLAUDE.md"
            p.write_text(CLAUDE.replace("- Worktrees: `trees/`\n", ""))
            self.assertEqual(al.worktrees_dir(p.read_text()), "")


class OwnershipTest(unittest.TestCase):
    def test_a_row_matches_by_task_item_as_well_as_owner(self):
        row = al.OwnerRow("Security audit", ["audit-upload"])
        self.assertTrue(al.owns(row, "Security audit"))
        self.assertFalse(al.owns(row, "Draft release notes"))

    def test_owner_matches_a_row_ignoring_the_ref_and_the_parenthetical(self):
        row = al.OwnerRow("demo-fixes [b8fca1] (fix 5, from 11:13)", ["openemr-agent-demo-fixes"])
        self.assertTrue(al.owns(row, "demo-fixes"))
        self.assertTrue(al.owns(row, "demo-fixes [b8fca1]"))
        self.assertFalse(al.owns(row, "demo"))
        self.assertFalse(al.owns(row, "architecture"))


class ReadabilityTest(unittest.TestCase):
    def test_a_refusal_is_reported_once_however_many_tasks_name_it(self):
        rows = (
            "| One | robin | done 10:00 | 09:00 |  | Checklist: One |\n"
            "| Two | robin | done 10:01 | 09:00 |  | Checklist: Two |\n"
            "| Three | robin | done 10:02 | 09:00 |  | Checklist: Three |"
        )
        tmp, root = workspace(rows, "\n".join(started(t, "wt-not-here") for t in ("One", "Two", "Three")))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(sum(1 for line in report.lines if "wt-not-here" in line), 1, report.lines)

    def test_a_long_task_item_is_cut_short_in_the_reopen_line(self):
        item = "Delete agent-parked (superseded; secrets) and the client-JSON copy in the scratchpad. 11:13 assigned. 11:53 deleted (62 MB); path never recorded"
        tmp, root = workspace(
            f"| {item} | robin | done 10:00 | 09:00 |  | Checklist: Delete |",
            started(item, "wt-unmerged"),
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        line = str(report.reopen[0])
        self.assertLess(len(line), 140, line)
        self.assertIn("Delete agent-parked", line)


class ContainmentTest(unittest.TestCase):
    def test_a_worktree_outside_the_root_is_refused_not_probed(self):
        tmp, root = workspace(
            "| Escape | robin | done 10:00 | 09:00 |  | Checklist: Escape |",
            started("Escape", "../../../etc"),
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertTrue(any("outside" in line for line in report.lines), report.lines)
        self.assertEqual(report.reopen, [])

    def test_a_missing_worktree_is_reported_not_silent(self):
        tmp, root = workspace(
            "| Gone | robin | done 10:00 | 09:00 |  | Checklist: Gone |",
            started("Gone", "wt-not-here"),
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertTrue(any("wt-not-here" in line for line in report.lines), report.lines)


class AuditTest(unittest.TestCase):
    def test_done_on_main_stays_done(self):
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            started("Landed work", "wt-merged"),
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.reopen, [])

    def test_done_on_an_unmerged_branch_must_reopen(self):
        tmp, root = workspace(
            "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |",
            started("Open branch work", "wt-unmerged"),
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Open branch work"])
        self.assertIn("not on main", report.reopen[0].why)

    def test_a_done_task_the_log_records_as_user_approved_stays_done(self):
        # #547: a forge closure the user approved is recorded in the Log, and the audit honours the record.
        tmp, root = workspace(
            "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |",
            started("Open branch work", "wt-unmerged"),
        )
        self.addCleanup(tmp.cleanup)
        tracker = root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text() + tl.approved_close_line(
            "10:05", "Open branch work", "the forge closed its issue; work stays on the branch") + "\n")
        self.assertEqual(al.audit(root, "2026-09-17").reopen, [])

    def test_an_approval_recorded_for_another_task_does_not_clear_this_one(self):
        tmp, root = workspace(
            "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |",
            started("Open branch work", "wt-unmerged"),
        )
        self.addCleanup(tmp.cleanup)
        tracker = root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text() + tl.approved_close_line(
            "10:05", "Some other task", "the forge closed its issue") + "\n")
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Open branch work"])

    def test_done_with_uncommitted_work_must_reopen(self):
        tmp, root = workspace(
            "| Unsaved work | robin | done 10:00 | 09:00 |  | Checklist: Unsaved work |",
            started("Unsaved work", "wt-dirty"),
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Unsaved work"])
        self.assertIn("uncommitted", report.reopen[0].why)

    def test_a_task_with_no_tree_is_never_reopened(self):
        tmp, root = workspace(
            "| Decide the merge order | robin | done 10:00 | 09:00 |  | Checklist: Decide |",
            "",
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.reopen, [])

    def test_a_running_task_on_an_unmerged_branch_is_not_reopened(self):
        tmp, root = workspace(
            "| Still going | robin | running 09:30 | 09:00 |  | Checklist: Still going |",
            started("Still going", "wt-unmerged"),
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.reopen, [])

    def test_two_done_tasks_one_landed_one_not(self):
        rows = (
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |\n"
            "| Open branch work | sam | done 10:30 | 09:00 |  | Checklist: Open branch work |"
        )
        tmp, root = workspace(rows, started("Landed work", "wt-merged") + "\n" + started("Open branch work", "wt-unmerged"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Open branch work"])

    def test_a_branch_merged_on_the_forge_is_landed_though_local_main_lags(self):
        """#567: the forge moved main, and origin/main with it (the push from the tree updates the clone's shared ref);
        local main, which nothing moves, still lags."""
        tmp, root = workspace(
            "| Open branch work | sam | done 10:30 | 09:00 |  | Checklist: Open branch work |",
            started("Open branch work", "wt-unmerged"),
        )
        self.addCleanup(tmp.cleanup)
        clone, tree = root / "repo", root / "trees" / "wt-unmerged"
        main = git("rev-parse", "main", cwd=clone)
        git("push", "origin", "HEAD:main", cwd=tree)
        self.assertEqual(git("rev-parse", "main", cwd=clone), main)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.reopen, [])
        self.assertTrue(any(line.startswith("wt-unmerged (feat/open): on main") for line in report.lines), report.lines)

    def test_a_cited_sha_merged_on_the_forge_is_landed_though_local_main_lags(self):
        """#567 review: `landed` reads origin/main too. The tree moves on past the pushed commit, so only the cited
        sha, on origin/main alone, can clear the task."""
        tmp, root = workspace("| Open branch work | sam | @STATE@ | 09:00 |  | Checklist: Open branch work |",
                              started("Open branch work", "wt-unmerged"))
        self.addCleanup(tmp.cleanup)
        tree = root / "trees" / "wt-unmerged"
        sha = git("rev-parse", "--short", "HEAD", cwd=tree).strip()
        git("push", "origin", "HEAD:main", cwd=tree)
        git("commit", "--allow-empty", "-m", "more work", cwd=tree)
        p = root / "daily" / "2026-09-17-tracker.md"
        p.write_text(p.read_text().replace("@STATE@", f"done 10:30 {sha}"))
        self.assertEqual(al.audit(root, "2026-09-17").reopen, [])

    def test_a_removed_tree_whose_branch_merged_on_the_forge_is_cleaned_up(self):
        """#567 review: `missing_tree` reads origin/main too."""
        tmp, root = workspace("| Open branch work | sam | waiting | 09:00 |  | Checklist: Open branch work |",
                              started("Open branch work", "wt-unmerged"))
        self.addCleanup(tmp.cleanup)
        clone, tree = root / "repo", root / "trees" / "wt-unmerged"
        git("push", "origin", "HEAD:main", cwd=tree)
        git("worktree", "remove", "--force", str(tree), cwd=clone)
        git("branch", "-m", "feat/open", "wt-unmerged", cwd=clone)
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "wt-unmerged" in ln)
        self.assertIn("is on main; merged and cleaned up", line)

    def test_a_tag_named_like_a_removed_trees_branch_does_not_stand_in_for_it(self):
        """#567 review: `missing_tree` asks about the branch itself, so a tag of the same name on main does not clear it."""
        tmp, root = workspace("| Open branch work | sam | waiting | 09:00 |  | Checklist: Open branch work |",
                              started("Open branch work", "wt-unmerged"))
        self.addCleanup(tmp.cleanup)
        clone = root / "repo"
        git("worktree", "remove", "--force", str(root / "trees" / "wt-unmerged"), cwd=clone)
        git("branch", "-m", "feat/open", "wt-unmerged", cwd=clone)
        git("tag", "wt-unmerged", "main", cwd=clone)
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "wt-unmerged" in ln)
        self.assertIn("is not on main", line)

    def test_a_branch_integrated_on_local_main_before_its_push_is_landed(self):
        """#567 review: local main ahead of origin/main (integrated, not yet pushed) still holds the work."""
        tmp, root = workspace(
            "| Open branch work | sam | done 10:30 | 09:00 |  | Checklist: Open branch work |",
            started("Open branch work", "wt-unmerged"),
        )
        self.addCleanup(tmp.cleanup)
        clone = root / "repo"
        git("merge", "--ff-only", "feat/open", cwd=clone)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.reopen, [])
        self.assertTrue(any(line.startswith("wt-unmerged (feat/open): on main") for line in report.lines), report.lines)


class ViewReuseTest(unittest.TestCase):
    """#548: the audit opens one git view per tree and reuses it for every read about that tree — branch, status,
    merge, the cited sha — instead of building and removing one view per read."""

    def test_an_audit_builds_a_low_constant_number_of_views_per_tree(self):
        tmp, root = workspace(
            "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |",
            started("Open branch work", "wt-unmerged"),
        )
        self.addCleanup(tmp.cleanup)
        # A cited sha, so `landed`'s reads ride the same per-tree view too.
        sha = git("rev-parse", "HEAD", cwd=root / "trees" / "wt-unmerged")
        tracker = root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text().replace("done 10:00 |", f"done 10:00 {sha} |"))
        builds: list[Path] = []
        real = git_view._opened

        @contextlib.contextmanager
        def counting(path, env, base, deadline):
            builds.append(path)
            with real(path, env, base, deadline) as view:
                yield view

        with patch.object(git_view, "_opened", counting):
            report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Open branch work"], "the audit still answers")
        # Three trees on disk (wt-merged, wt-unmerged, wt-dirty), one view each; the used-to-be ~10-12 builds per tree.
        self.assertLessEqual(len(builds), 4, f"one view per tree, not per read: {len(builds)} built for {builds}")


class OnMainTest(unittest.TestCase):
    """#567 review: either main holding the commit lands it; "not on main" needs every main that exists to say so, so a
    comparison git could not make stays unknown. Each main is found by its exact ref name and compared by object id."""

    ORIGIN, LOCAL = "1" * 40, "2" * 40

    def on_main(self, origin: int | None, local: int | None, lookup: int = 0) -> int:
        """`origin` and `local` are merge-base's answer for each main, None when that main does not exist."""
        answers = {self.ORIGIN: origin, self.LOCAL: local}

        def git(args, tree, view=None):
            if args[0] == "for-each-ref":
                self.assertEqual(args[2:], [git_trees.REF, git_trees.LOCAL_REF])
                refs = ((git_trees.REF, self.ORIGIN), (git_trees.LOCAL_REF, self.LOCAL))
                return lookup, "\n".join(f"{name} {oid}" for name, oid in refs if answers[oid] is not None)
            self.assertEqual(args[:3], ["merge-base", "--is-ancestor", "abc"])
            return answers[args[3]], ""
        with patch.object(al.git_trees, "git", git):
            return al._on_main("abc", Path("."))

    def test_either_main_holding_it_is_landed(self):
        self.assertEqual((self.on_main(0, 1), self.on_main(1, 0), self.on_main(128, 0)), (0, 0, 0))

    def test_both_mains_saying_no_is_not_on_main(self):
        self.assertEqual(self.on_main(1, 1), 1)

    def test_a_failed_comparison_beside_a_no_is_unknown(self):
        self.assertEqual(self.on_main(128, 1), 128)
        self.assertEqual(self.on_main(1, 128), 128)

    def test_with_no_origin_main_local_main_decides(self):
        self.assertEqual((self.on_main(None, 1), self.on_main(None, 0)), (1, 0))

    def test_with_no_main_at_all_it_is_unknown(self):
        self.assertEqual(self.on_main(None, None), 128)

    def test_a_failed_lookup_is_unknown(self):
        self.assertEqual(self.on_main(1, 1, lookup=128), 128)

    def test_a_branch_named_like_origin_main_does_not_stand_in_for_it(self):
        # With no refs/remotes/origin/main, git's lookup would take a branch called `refs/remotes/origin/main`.
        tmp, root = workspace("| Open branch work | sam | done 10:30 | 09:00 |  | Checklist: Open branch work |",
                              started("Open branch work", "wt-unmerged"))
        self.addCleanup(tmp.cleanup)
        clone = root / "repo"
        git("update-ref", "-d", git_trees.REF, cwd=clone)
        git("branch", git_trees.REF, "feat/open", cwd=clone)
        self.assertEqual(al._on_main("refs/heads/feat/open", clone), 1)


class PushGapTest(unittest.TestCase):
    def test_main_even_with_origin_is_reported_as_even(self):
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            started("Landed work", "wt-merged"),
        )
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertTrue(any("origin/main" in line and "even" in line for line in report.lines), report.lines)

    def test_a_commit_on_main_not_pushed_is_reported_as_unpushed(self):
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            started("Landed work", "wt-merged"),
        )
        self.addCleanup(tmp.cleanup)
        clone = root / "repo"
        (clone / "later.txt").write_text("later\n")
        git("add", "later.txt", cwd=clone)
        git("commit", "-m", "later", cwd=clone)
        report = al.audit(root, "2026-09-17")
        self.assertTrue(any("unpushed" in line for line in report.lines), report.lines)

    def test_pass_push_gap_skips_unreadable_then_names_when_none_readable(self):
        """#537: `_pass_push_gap` takes the first readable fact, else names the first tree."""
        a, b = Path("/tmp/wt-a"), Path("/tmp/wt-b")
        answers = {a: al.UNREADABLE_GAP, b: "main abc even with origin/main"}
        with patch.object(al, "_push_gap", side_effect=lambda path, view=None: answers[path]):
            self.assertEqual(al._pass_push_gap([a, b], lambda p: None, {a: "wt-a", b: "wt-b"}),
                             "main abc even with origin/main")
            answers[b] = al.UNREADABLE_GAP
            self.assertEqual(al._pass_push_gap([a, b], lambda p: None, {a: "wt-a", b: "wt-b"}),
                             "wt-a: main vs origin/main unknown")
            answers.update({a: "main abc123 vs origin/main unknown", b: "main abc even with origin/main"})
            self.assertEqual(al._pass_push_gap([a, b], lambda p: None, {a: "wt-a", b: "wt-b"}),
                             "main abc even with origin/main")


class ArgvTest(unittest.TestCase):
    def test_git_is_called_with_a_list_and_never_through_a_shell(self):
        source = (Path(__file__).resolve().parent.parent / "scripts" / "audit_tasks.py").read_text()
        self.assertNotIn("shell=True", source)
        self.assertIn('"--porcelain"', source)

    def test_exit_code_counts_the_rows_to_reopen(self):
        tmp, root = workspace(
            "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |",
            started("Open branch work", "wt-unmerged"),
        )
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.main(["--root", str(root), "--date", "2026-09-17"]), 1)

    def test_a_clean_tracker_exits_zero(self):
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            started("Landed work", "wt-merged"),
        )
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.main(["--root", str(root), "--date", "2026-09-17"]), 0)


if __name__ == "__main__":
    unittest.main()


class OrphanJoinTest(unittest.TestCase):
    """Finding 47. The audit reports per tree, and a tree that lost its writer is a fact about owners.

    Five sessions left the registry in fifty minutes one night and three left uncommitted work. The
    script found none of it, because nothing here had ever read `## Sessions`.
    """

    DIRTY = "| Unsaved work | sam | waiting | 09:00 |  | Checklist: Unsaved work |"
    OWNS = started("Unsaved work", "wt-dirty")

    def test_uncommitted_work_whose_owner_is_not_a_session_is_named(self) -> None:
        tmp, root = workspace(self.DIRTY, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(len(report.orphans), 1, report.lines)
        line = str(report.orphans[0])
        self.assertIn("wt-dirty", line)
        self.assertIn("sam", line)
        self.assertIn("uncommitted", line)

    def test_an_orphan_line_escapes_controls_in_the_branch(self) -> None:
        tmp, root = workspace(self.DIRTY, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        real = git_trees.read_state
        with patch.object(git_trees, "read_state",
                          side_effect=lambda *a, **k: replace(real(*a, **k), branch="feat/x\u202ey")):
            report = al.audit(root, "2026-09-17")
        line = str(report.orphans[0])
        self.assertNotIn("\u202e", line)
        self.assertIn("\\u202e", line)

    def test_a_live_owner_is_not_an_orphan(self) -> None:
        tmp, root = workspace(self.DIRTY, self.OWNS, sessions=session_row("sam"))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_the_ref_a_listing_shows_does_not_defeat_the_join(self) -> None:
        """`_bare()` already strips `[a1b2c3]`; this is the one place both sides of the join use it."""
        tmp, root = workspace(self.DIRTY, self.OWNS, sessions=session_row("sam [a1b2c3]"))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_no_sessions_section_makes_no_orphan_claims(self) -> None:
        """A listing is not a roster, and neither is an empty table. With no roster, say nothing."""
        tmp, root = workspace(self.DIRTY, self.OWNS)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    OPEN = "| Open branch work | sam | waiting | 09:00 |  | Checklist: Open branch work |"
    OPEN_OWNS = started("Open branch work", "wt-unmerged")

    def test_pushed_work_is_not_an_orphan_however_gone_its_owner(self) -> None:
        """An unmerged branch a remote holds is recoverable by name. One only this tree holds is not (#43)."""
        tmp, root = workspace(self.OPEN, self.OPEN_OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        git("push", "-u", "origin", "feat/open", cwd=root / "trees" / "wt-unmerged")
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_unpushed_work_is_an_orphan_when_its_owner_is_gone(self) -> None:
        """#43: a commit that existed only locally, with no upstream, and the audit said nothing."""
        tmp, root = workspace(self.OPEN, self.OPEN_OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(len(report.orphans), 1, report.lines)
        line = str(report.orphans[0])
        self.assertIn("wt-unmerged", line)
        self.assertIn("1 not on origin/main", line)
        self.assertIn("no upstream", line)

    def test_a_commit_ahead_of_its_upstream_is_counted(self) -> None:
        tmp, root = workspace(self.OPEN, self.OPEN_OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        tree = root / "trees" / "wt-unmerged"
        git("push", "-u", "origin", "feat/open", cwd=tree)
        (tree / "more.txt").write_text("more\n")
        git("add", "more.txt", cwd=tree)
        git("commit", "-m", "more", cwd=tree)
        [orphan] = al.audit(root, "2026-09-17").orphans
        self.assertIn("1 unpushed", str(orphan))

    # A one-shot's started line is keyed on its task, which is never a session.
    ONE_SHOT = "| Unsaved work | impl-01 | running 09:10 | 09:00 |  | Checklist: Unsaved work |"
    TASK_KEYED = started("Unsaved work", "wt-dirty")

    def launcher(self, root: Path, pid: int) -> None:
        d = root / "trees/wt-dirty/.chief-of-stuff"
        d.mkdir(exist_ok=True)
        (d / ".gitignore").write_text("*\n")
        (d / "one-shot.pid").write_text(f"{pid} Unsaved work\n")

    def test_a_running_one_shots_tree_is_not_an_orphan(self) -> None:
        tmp, root = workspace(self.ONE_SHOT, self.TASK_KEYED, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.launcher(root, os.getpid())
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_a_one_shot_handed_to_the_user_is_not_an_orphan(self) -> None:
        # After a review hold the task is the user's, and its tree waits for them.
        tmp, root = workspace(self.ONE_SHOT.replace("impl-01 | running 09:10", "Robin | waiting"), self.TASK_KEYED,
                              sessions=session_row("sam"))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_a_one_shot_whose_launcher_died_is_still_an_orphan(self) -> None:
        tmp, root = workspace(self.ONE_SHOT, self.TASK_KEYED, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        self.launcher(root, gone.pid)
        self.assertEqual(len(al.audit(root, "2026-09-17").orphans), 1)

    def test_the_count_reaches_the_summary_line(self) -> None:
        tmp, root = workspace(self.DIRTY, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.assertIn("orphaned=1", al.audit(root, "2026-09-17").lines[-1])


class OwnershipRowKeyedOnShortNameTest(unittest.TestCase):
    """#553: dispatch keys a File ownership row on the item's colon prefix or board short name
    (`task_keys`), and the audit read only the full item and the Tasks `name` — so a row the
    coordinator had rewritten to the short form joined nothing, and every done task behind it was
    orphaned the moment the Sessions table grew a row. The audit accepts dispatch's key set, and the
    empty-roster escape stops covering rows when there are Sessions rows to ask instead.
    """

    ITEM = "Security audit: the upload handler, every path, findings into notes/audit.md"
    TASK = f"| {ITEM} | Robin | done 10:00 | 09:00 |  | Checklist: Security audit |"
    SHORT_KEYED = started("Security audit", "wt-dirty")
    MISKEYED = started("ghost (unrelated)", "wt-dirty")

    def row(self, context: str, tree: str = "wt-a") -> al.OwnerRow:
        return al.OwnerRow(context, [tree])

    def test_a_row_keyed_on_a_short_name_is_not_orphaned_when_sessions_has_rows(self) -> None:
        """The issue's repro: a done one-shot's short-keyed row, one just-spawned session row, ref empty."""
        tmp, root = workspace(self.TASK, self.SHORT_KEYED, sessions=session_row("sam", ref=""))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.orphans, [], report.lines)

    def test_a_row_keyed_on_a_short_colon_head_is_not_orphaned_either(self) -> None:
        """A head under 12 characters is one `short_name` does not cut to, so the colon prefix is the key."""
        item = "Tab check: confirm this session came up in a tab and change nothing at all"
        task = f"| {item} | Robin | done 10:00 | 09:00 |  | Checklist: Tab check |"
        tmp, root = workspace(task, started("Tab check", "wt-dirty"),
                              sessions=session_row("sam", ref=""))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").orphans, [])

    def test_keyed_on_reads_the_colon_prefix_and_short_name(self) -> None:
        """Dispatch's key set (`task_keys`), read as audit reads prose: bare, emphasis and refs stripped."""
        item = "Security audit: the upload handler"
        self.assertTrue(al.keyed_on(self.row("Security audit"), item, ""))
        self.assertTrue(al.keyed_on(self.row("**Security audit** [a1b2c3]"), item, ""))
        self.assertFalse(al.keyed_on(self.row("Security"), item, ""))

    def test_a_row_keyed_on_nothing_is_still_orphaned_when_sessions_has_rows(self) -> None:
        """The widening must not blanket-accept: a context no key of the task names stays dead."""
        tmp, root = workspace(self.TASK, self.MISKEYED, sessions=session_row("sam"))
        self.addCleanup(tmp.cleanup)
        [orphan] = al.audit(root, "2026-09-17").orphans
        self.assertIn("wt-dirty", str(orphan))

    def test_sessions_rows_that_name_no_one_do_not_re_arm_the_escape(self) -> None:
        """The escape answers for an empty table; a table with rows is asked, and a row keyed on
        nothing — no task key, no listed context, no ref — is dead."""
        tmp, root = workspace(self.TASK, self.MISKEYED, sessions=session_row(""))
        self.addCleanup(tmp.cleanup)
        [orphan] = al.audit(root, "2026-09-17").orphans
        self.assertIn("wt-dirty", str(orphan))
        self.assertIn("is not in ## Sessions", str(orphan))


class TreesOnDiskTest(unittest.TestCase):
    """#43. `visit()` reached only trees a File ownership row named, so a gone session's tree that no
    row named was never looked at, and the audit printed `trees=0` over two trees with work at risk.
    The trees on disk are the entry point now; rows, registrations and launchers only say whose they are.
    """

    TASK = "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |"

    def register(self, root: Path, name: str, pid: int, tree: str) -> None:
        folder = root / ".chief-of-stuff" / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps(
            {"pid": pid, "name": name, "runtime": "codex", "worktree": str(root / "trees" / tree)}))

    def dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        return proc.pid

    def test_a_gone_workers_unpushed_tree_no_row_names_is_orphaned(self) -> None:
        """The acceptance case: one committed, unpushed commit; no started line; its worker's PID gone."""
        tmp, root = workspace(self.TASK, "", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.register(root, "impl-7", self.dead_pid(), "wt-unmerged")
        report = al.audit(root, "2026-09-17")
        [orphan] = [o for o in report.orphans if "wt-unmerged" in str(o)]
        self.assertIn("1 not on origin/main", str(orphan))
        self.assertIn("impl-7", str(orphan))
        self.assertIn("no upstream", str(orphan))

    def test_a_live_workers_tree_is_not_orphaned(self) -> None:
        tmp, root = workspace(self.TASK, "", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.register(root, "impl-7", os.getpid(), "wt-unmerged")
        report = al.audit(root, "2026-09-17")
        self.assertFalse(any("wt-unmerged" in str(o) for o in report.orphans + report.unclaimed), report.lines)

    def test_at_risk_work_nothing_claims_is_unclaimed(self) -> None:
        tmp, root = workspace(self.TASK, "", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.orphans, [], report.lines)
        self.assertEqual(sorted(str(u).split(" — ")[0] for u in report.unclaimed),
                         ["unclaimed work: wt-dirty (feat/dirty)", "unclaimed work: wt-unmerged (feat/open)"])
        self.assertIn(" unclaimed=2", report.lines[-1])

    def test_unclaimed_needs_no_sessions_table(self) -> None:
        """No roster means no claim that an owner is gone; a tree nobody names needs no owner to be at risk."""
        tmp, root = workspace(self.TASK, "")
        self.addCleanup(tmp.cleanup)
        self.assertEqual(len(al.audit(root, "2026-09-17").unclaimed), 2)

    def test_every_tree_on_disk_is_counted(self) -> None:
        tmp, root = workspace(self.TASK, "", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertIn(" trees=3 ", report.lines[-1])
        self.assertTrue(any(ln.startswith("wt-merged (landed): ") for ln in report.lines), report.lines)

    def test_safe_trees_raise_nothing(self) -> None:
        tmp, root = workspace(self.TASK, "", sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertFalse(any("wt-merged" in str(f) for f in report.orphans + report.unclaimed), report.lines)

    def test_no_unclaimed_count_when_there_is_none(self) -> None:
        tmp, root = workspace(self.TASK + "\n| Other work | robin | waiting | 09:00 |  | c |",
                              started("Landed work", "wt-dirty") + "\n" + started("Other work", "wt-unmerged"),
                              sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.assertNotIn("unclaimed=", al.audit(root, "2026-09-17").lines[-1])


class MissingTreeTest(unittest.TestCase):
    """Finding 46. A missing tree means two opposite things and the script printed one line for both.

    `openemr-agent-smart` was gone because its branch merged and the session tidied up.
    `openemr-agent-defects-140c0b2` was gone because the row carried a planned name and the tree was
    made without the suffix. One is housekeeping; the other is a task pointing at nothing.
    """

    def remove(self, root: Path, name: str, branch: str = "") -> None:
        git("worktree", "remove", "--force", str(root / "trees" / name), cwd=root / "repo")
        if branch:
            # A started line names no branch (#1039), so only a branch named like the tree is found.
            git("branch", "-m", branch, name, cwd=root / "repo")

    def test_a_merged_branch_with_no_tree_is_routine(self) -> None:
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            started("Landed work", "wt-merged"))
        self.addCleanup(tmp.cleanup)
        self.remove(root, "wt-merged", "landed")
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "wt-merged" in ln)
        self.assertIn("merged and cleaned up", line)
        self.assertIn("on main", line)
        self.assertNotIn("never", line)

    def test_an_unmerged_branch_with_no_tree_is_the_alarming_one(self) -> None:
        tmp, root = workspace(
            "| Open branch work | sam | waiting | 09:00 |  | Checklist: Open branch work |",
            started("Open branch work", "wt-unmerged"))
        self.addCleanup(tmp.cleanup)
        self.remove(root, "wt-unmerged", "feat/open")
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "wt-unmerged" in ln)
        self.assertIn("branch wt-unmerged", line)
        self.assertIn("not on main", line)

    def test_a_name_with_no_branch_behind_it_was_never_created(self) -> None:
        tmp, root = workspace(
            "| Planned work | sam | waiting | 09:00 |  | Checklist: Planned work |",
            started("Planned work", "wt-never-made"))
        self.addCleanup(tmp.cleanup)
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "wt-never-made" in ln)
        self.assertIn("no branch", line)

    def test_with_no_tree_left_to_ask_git_in_the_script_says_so(self) -> None:
        """Every answer here is git's answer, and git needs a repository to be asked in."""
        tmp, root = workspace(
            "| Planned work | sam | waiting | 09:00 |  | Checklist: Planned work |",
            started("Planned work", "wt-never-made"))
        self.addCleanup(tmp.cleanup)
        for name in ("wt-merged", "wt-unmerged", "wt-dirty"):
            self.remove(root, name)
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "wt-never-made" in ln)
        self.assertIn("no worktree", line)
        self.assertNotIn("no branch", line)


class UnclaimedOwnershipRowTest(unittest.TestCase):
    """A tree reached only through a task is a tree nobody can reach once the task lets go.

    Live at 17:20 the workspace held three trees whose File ownership context was `nobody (…,
    orphaned …)`. No task owner is `nobody`, so `rows_for` matched none of them and the audit walked
    straight past all three — the very trees finding 47 was written about.
    """

    OWNS = started("Landed work", "wt-merged") + "\n" + started("The TTL fix", "wt-dirty")
    TASK = "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |"

    def test_a_row_no_task_claims_is_still_audited(self) -> None:
        tmp, root = workspace(self.TASK, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertTrue(any("wt-dirty" in ln for ln in report.lines), report.lines)

    def test_and_its_orphaned_work_is_named(self) -> None:
        tmp, root = workspace(self.TASK, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(len(report.orphans), 1, report.lines)
        self.assertIn("the ttl fix", str(report.orphans[0]))

    def test_an_unclaimed_row_never_reopens_a_task(self) -> None:
        """It belongs to no task, so there is no row to reopen and nothing to infer about one."""
        tmp, root = workspace(self.TASK, self.OWNS, sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(al.audit(root, "2026-09-17").reopen, [])

    def test_a_tree_is_reported_once_however_many_rows_reach_it(self) -> None:
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            started("Landed work", "wt-merged") + "\n" + started("Earlier work", "wt-merged"),
            sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(sum(1 for ln in report.lines if ln.startswith("wt-merged ")), 1, report.lines)


class MainShaTest(unittest.TestCase):
    """House rule 5 (CIMP, 2026-09-18 17:18): a green branch is not evidence, a green main is.

    The script cannot watch a suite run, so it does not claim to. What it can do is name the commit
    the claim has to be about, so "suite green on main" becomes checkable instead of unfalsifiable:
    if main has moved since, the evidence is about a main that no longer exists.
    """

    def test_the_audit_names_the_commit_on_main_a_claim_must_pin_to(self) -> None:
        tmp, root = workspace(
            "| Landed work | robin | done 10:00 | 09:00 |  | Checklist: Landed work |",
            started("Landed work", "wt-merged"))
        self.addCleanup(tmp.cleanup)
        sha = git("rev-parse", "--short", "main", cwd=root / "repo")
        line = next(ln for ln in al.audit(root, "2026-09-17").lines if "origin/main" in ln)
        self.assertIn(sha, line)

    def test_with_no_tree_on_disk_there_is_no_sha_to_report(self) -> None:
        """Every answer here is git's answer, and there is nowhere left to ask."""
        tmp, root = workspace(
            "| Planned work | sam | waiting | 09:00 |  | Checklist: Planned work |",
            started("Planned work", "wt-never-made"))
        self.addCleanup(tmp.cleanup)
        # The audit walks the trees on disk (#43), so none may be left there.
        for name in ("wt-merged", "wt-unmerged", "wt-dirty"):
            git("worktree", "remove", "--force", str(root / "trees" / name), cwd=root / "repo")
        self.assertFalse([ln for ln in al.audit(root, "2026-09-17").lines if "origin/main" in ln])


STOP = """Stop: permission
Task: {task}
Lands on: Robin
Costs: D4 does not reach main, and the Final submission is short a deliverable
"""


class StopTest(unittest.TestCase):
    """A stop is an artifact, not a message: a message is lost if nobody reads it, a file is not."""

    def setUp(self):
        self.tmp, self.root = workspace(
            "| Merge D4 | impl-3 | running 02:20 | 09:00 |  | c |",
            started("Merge D4", "wt-unmerged"),
            session_row("impl-3"))
        stop = self.root / "trees" / "wt-unmerged" / al.PROMPT_DIR
        stop.mkdir(parents=True)
        (stop / "stop.md").write_text(STOP.format(task="Merge D4"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stop_against_a_running_task_is_reported(self):
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual([s.task for s in report.stopped], ["Merge D4"])
        self.assertEqual(report.stopped[0].lands_on, "Robin")

    def test_the_line_says_the_kind_and_who_it_lands_on(self):
        line = str(al.audit(self.root, "2026-09-17").stopped[0])
        self.assertIn("stopped:", line)
        self.assertIn("permission", line)
        self.assertIn("Robin", line)

    def test_the_summary_counts_it_and_the_exit_code_does_too(self):
        report = al.audit(self.root, "2026-09-17")
        self.assertIn("stopped=1", report.lines[-1])
        self.assertEqual(al.main(["--root", str(self.root), "--date", "2026-09-17"]), 1)

    def test_a_stop_whose_task_is_already_open_is_not_a_fault(self):
        tracker = self.root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text().replace("running 02:20", "open"))
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual(report.stopped, [])
        self.assertIn("stopped=0", report.lines[-1])

    def test_a_tree_with_no_stop_says_nothing(self):
        (self.root / "trees" / "wt-unmerged" / al.PROMPT_DIR / "stop.md").unlink()
        self.assertEqual(al.audit(self.root, "2026-09-17").stopped, [])


class StopEdgesTest(unittest.TestCase):
    """A session that half-wrote a stop still stopped, and a stop somebody acted on is not a fault."""

    def setUp(self):
        self.tmp, self.root = workspace(
            "| Merge D4 | impl-3 | running 02:20 | 09:00 |  | c |",
            started("Merge D4", "wt-unmerged"),
            session_row("impl-3"))
        self.dir = self.root / "trees" / "wt-unmerged" / al.PROMPT_DIR
        self.dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stop_with_no_kind_is_reported_as_unstated_not_ignored(self):
        (self.dir / "stop.md").write_text("Task: Merge D4\nLands on: Robin\n")
        line = str(al.audit(self.root, "2026-09-17").stopped[0])
        self.assertIn("kind unstated", line)

    def test_a_stop_naming_nobody_says_so(self):
        (self.dir / "stop.md").write_text("Stop: scope\nTask: Merge D4\n")
        self.assertIn("lands on nobody it named", str(al.audit(self.root, "2026-09-17").stopped[0]))

    def test_an_empty_stop_file_still_counts(self):
        (self.dir / "stop.md").write_text("")
        self.assertEqual(len(al.audit(self.root, "2026-09-17").stopped), 1)


QUEUE = """
## Decision queue

| # | decision | why it is next | who is blocked |
|---|---|---|---|
{rows}
"""


def with_queue(root: Path, rows: str) -> None:
    t = root / "daily" / "2026-09-17-tracker.md"
    t.write_text(t.read_text() + QUEUE.format(rows=rows))


class DecisionQueueTest(unittest.TestCase):
    """Emitting N asks is emitting none, so the queue has to be one ordered list with a payload per row."""

    def setUp(self):
        self.tmp, self.root = workspace("| A | Robin | open | 09:00 |  | c |", "")
        self.addCleanup(self.tmp.cleanup)

    def test_a_clean_queue_reports_its_head_and_no_fault(self):
        with_queue(self.root, "| 1 | Ship it | it blocks two | impl-2 |\n| 2 | Later | it does not | nobody |")
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual(report.queue, [])
        self.assertIn("queued=2", report.lines[-1])
        self.assertIn("next decision: 1 — Ship it", "\n".join(report.lines))

    def test_two_rows_with_the_same_number_are_a_fault(self):
        with_queue(self.root, "| 1 | Ship it | x | y |\n| 4 | One | x | y |\n| 4 | Another | x | y |")
        self.assertIn("number 4 twice", "\n".join(str(q) for q in al.audit(self.root, "2026-09-17").queue))

    def test_an_answered_row_that_never_left_is_a_fault(self):
        with_queue(self.root, "| ~~1~~ | ~~Done~~ **ANSWERED 03:29** | — | — |\n| 2 | Ship it | x | y |")
        self.assertIn("answered", "\n".join(str(q) for q in al.audit(self.root, "2026-09-17").queue))

    def test_a_row_with_no_payload_cannot_be_decided(self):
        with_queue(self.root, "| 1 | Ship it |  |  |")
        self.assertIn("names neither", "\n".join(str(q) for q in al.audit(self.root, "2026-09-17").queue))

    def test_faults_reach_the_exit_code(self):
        with_queue(self.root, "| 4 | One | x | y |\n| 4 | Another | x | y |")
        self.assertEqual(al.main(["--root", str(self.root), "--date", "2026-09-17"]), 1)

    def test_no_queue_section_says_nothing_at_all(self):
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual(report.queue, [])
        self.assertNotIn("queued=", report.lines[-1])

    def test_the_next_line_names_a_decision_that_opens_with_a_bold_headline(self):
        """`short_name` cutting inside a `**` pair returns a bare ellipsis, and the whole point of the line is the name."""
        with_queue(self.root, "| 3 | **Which surface carries the cache hit rate.** impl-1 measured first | x | y |")
        self.assertIn("next decision: 3 — Which surface carries the cache hit rate",
                      "\n".join(al.audit(self.root, "2026-09-17").lines))


class StopReadGuardTest(unittest.TestCase):
    """A stop file is written by a session and printed into somebody's terminal. It is checked like a dispatch field."""

    def setUp(self):
        self.tmp, self.root = workspace(
            "| Merge D4 | impl-3 | running 02:20 | 09:00 |  | c |",
            started("Merge D4", "wt-unmerged"),
            session_row("impl-3"))
        self.dir = self.root / "trees" / "wt-unmerged" / al.PROMPT_DIR
        self.dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stop_that_exists_but_cannot_be_read_still_counts(self):
        """Fail-open is the one thing this feature cannot do: a stop it could not read is still a stop."""
        (self.dir / "stop.md").mkdir()  # a directory where the file goes: exists, unreadable as text
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual(len(report.stopped), 1)
        self.assertIn("unreadable", str(report.stopped[0]))

    def test_an_escape_payload_never_reaches_the_terminal(self):
        (self.dir / "stop.md").write_text("Stop: permission\nTask: Merge D4\nLands on: \x1b]0;pwned\x07Robin\n")
        line = str(al.audit(self.root, "2026-09-17").stopped[0])
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\x07", line)

    def test_a_field_long_enough_to_bury_the_report_is_clipped(self):
        (self.dir / "stop.md").write_text("Stop: permission\nTask: Merge D4\nLands on: " + "x" * 5000 + "\n")
        self.assertLess(len(str(al.audit(self.root, "2026-09-17").stopped[0])), 1000)


class PushGapNamesMainTest(unittest.TestCase):
    """The one sha the CIMP gate rests on. `Verified` is re-checkable only if it names the branch it claims to."""

    def setUp(self):
        self.tmp, self.root = workspace("| A | Robin | open | 09:00 |  | c |", "")
        self.addCleanup(self.tmp.cleanup)
        self.main = git("rev-parse", "--short", "main", cwd=self.root / "repo")

    def test_it_names_mains_commit_and_not_the_worktrees(self):
        tree = self.root / "trees" / "wt-unmerged"
        self.assertNotEqual(git("rev-parse", "--short", "HEAD", cwd=tree), self.main)
        self.assertIn(f"main {self.main}", al._push_gap(tree))

    def test_the_count_and_the_sha_describe_the_same_branch(self):
        """The count resolved `main` while the sha resolved `HEAD`, so a right number vouched for a wrong name."""
        line = al._push_gap(self.root / "trees" / "wt-unmerged")
        self.assertIn(f"main {self.main}", line)
        self.assertIn("even with origin/main", line)


class BranchNameControlTest(unittest.TestCase):
    """#534: worker branch names with controls must not reach an audit line raw."""

    def test_state_escapes_line_separator_bidi_and_c1_in_the_branch(self):
        for what, ctl, shown in (("line separator", "\u2028", "\\u2028"),
                                 ("bidi override", "\u202e", "\\u202e"),
                                 ("C1 control", "\x9b", "\\x9b")):
            with self.subTest(what), \
                    patch.object(git_trees, "branch_of", return_value=f"feat/x{ctl}y"), \
                    patch.object(git_trees, "git", return_value=(0, "")), \
                    patch.object(al, "_merged", return_value=0):
                branch, _why = al._state(Path("/tmp/tree"))
                self.assertEqual(branch, f"feat/x{shown}y")
                self.assertEqual([c for c in branch if not c.isprintable()], [], repr(branch))
                line = al._tree_line("wt-x", branch, "")
                self.assertEqual([c for c in line if not c.isprintable()], [], repr(line))
                self.assertIn(shown, line)
                self.assertNotIn(ctl, line)


class RefJoinTest(unittest.TestCase):
    """Finding 112. A ref is minted once and survives a rename; a name is a tab title, and one changed
    at 03:21 on the owner's word that the worktree is the name. The join read the names, missed, and called
    a session that had committed five minutes earlier orphaned. Where both sides carry a ref, the ref
    is the join.
    """

    def audit(self, owner: str, sessions: str):
        tmp, root = workspace(f"| Unsaved work | {owner} | waiting | 09:00 |  | Checklist: Unsaved work |",
                              started("Unsaved work", "wt-dirty"), sessions=sessions)
        self.addCleanup(tmp.cleanup)
        return al.audit(root, "2026-09-17")

    def test_a_renamed_session_is_not_an_orphan(self) -> None:
        report = self.audit("sam [768194]",
                            session_row("**openemr-arch** (tab title `sam`)", ref="768194"))
        self.assertEqual(report.orphans, [], report.lines)

    def test_a_bolded_session_name_still_joins_when_there_is_no_ref(self) -> None:
        report = self.audit("sam", session_row("**sam**"))
        self.assertEqual(report.orphans, [], report.lines)

    def test_a_ref_no_session_carries_is_an_orphan_however_familiar_the_name(self) -> None:
        report = self.audit("sam [999999]",
                            session_row("sam", ref="a1b2c3"))
        self.assertEqual(len(report.orphans), 1, report.lines)
        self.assertIn("999999", str(report.orphans[0]))

    def test_with_no_ref_on_the_owner_the_name_still_decides(self) -> None:
        report = self.audit("sam", session_row("sam", ref="768194"))
        self.assertEqual(report.orphans, [], report.lines)

    def test_a_tracker_whose_sessions_carry_no_refs_falls_back_to_names(self) -> None:
        """An empty ref column is not a roster of nobody, the same way an empty table is not."""
        report = self.audit("sam [768194]", session_row("sam", ref=""))
        self.assertEqual(report.orphans, [], report.lines)


class OwnsRefTest(unittest.TestCase):
    """Finding 115. 112 put the ref in the roster join and stopped there. `owns()` — the join that
    attributes a task to an ownership row, and so to a tree — still read names, and today's tracker
    runs one ref under three names: `arch [768194]`, `architecture-review-setup [768194]`,
    `openemr-arch [768194]`. Four tasks matched no row, so no tree, so no reopen, orphan or stop
    check reached them. Silently unaudited is worse than wrongly audited.
    """

    def row(self, context: str) -> al.OwnerRow:
        return al.OwnerRow(context, ["wt-a"])

    def test_two_names_on_one_ref_are_one_context(self) -> None:
        self.assertTrue(al.owns(self.row("architecture-review-setup [768194]"), "openemr-arch [768194]"))

    def test_one_name_on_two_refs_is_two_contexts(self) -> None:
        self.assertFalse(al.owns(self.row("sam [aaaaaa]"), "sam [bbbbbb]"))

    def test_with_a_ref_on_one_side_only_the_name_still_decides(self) -> None:
        self.assertTrue(al.owns(self.row("sam"), "sam [aaaaaa]"))
        self.assertTrue(al.owns(self.row("sam [aaaaaa]"), "sam"))

class ReopenGroupsByTreeTest(unittest.TestCase):
    """Finding 116, option (c), from `gauntlet-b2` by way of `board-ux-improvements`.

    `audit_tasks` asks git once per tree and then fans that one fact across the tree's tasks, so
    thirteen reopen lines on the live tracker carried three facts. Grouping the *rendering* per tree
    is not a change to what is detected: every task that reopened still appears, the finding list is
    untouched, and the exit code is unchanged. It is the ratio that moves, and the ratio is lines per
    tree, which is what made the count climb as a session closed more tasks in one worktree.
    """

    TWO = ("| First | robin | done 10:00 | 09:00 |  | Checklist: First |\n"
           "| Second | robin | done 10:00 | 09:00 |  | Checklist: Second |")

    def report(self):
        tmp, root = workspace(self.TWO, started("First", "wt-unmerged") + "\n" + started("Second", "wt-unmerged"),
                              sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        return al.audit(root, "2026-09-17")

    def test_two_tasks_on_one_tree_render_as_one_line(self) -> None:
        lines = [l for l in self.report().lines if l.startswith("reopen:")]
        self.assertEqual(len(lines), 1, lines)

    def test_and_that_line_names_every_task_it_covers(self) -> None:
        """The invariant that makes this safe: grouping may not drop a task from the output."""
        report = self.report()
        rendered = "\n".join(l for l in report.lines if l.startswith("reopen:"))
        for r in report.reopen:
            self.assertIn(short_name(r.task), rendered)

    def test_the_finding_list_and_the_exit_code_are_untouched(self) -> None:
        report = self.report()
        self.assertEqual(sorted(r.task for r in report.reopen), ["First", "Second"])

    def test_the_summary_says_how_many_facts_and_how_many_tasks(self) -> None:
        summary = self.report().lines[-1]
        self.assertIn("reopen=1 tree", summary)
        self.assertIn("2 task", summary)

    def test_two_trees_stay_two_lines(self) -> None:
        tmp, root = workspace(
            "| First | robin | done 10:00 | 09:00 |  | Checklist: First |\n"
            "| Other | sam | done 10:00 | 09:00 |  | Checklist: Other |",
            started("First", "wt-unmerged") + "\n" + started("Other", "wt-dirty"),
            sessions=session_row("robin") + "\n" + session_row("sam"))
        self.addCleanup(tmp.cleanup)
        lines = [l for l in al.audit(root, "2026-09-17").lines if l.startswith("reopen:")]
        self.assertEqual(len(lines), 2, lines)

    def test_a_bolded_task_item_is_unmarked_before_it_is_shortened(self) -> None:
        """`short_name` cutting inside a `**` pair returns a bare ellipsis — the same defect fixed for
        the decision queue and never carried across. Grouping only made it visible: six tasks on one
        line rendered as six ellipses, and the old per-task output had been doing it all along."""
        tmp, root = workspace(
            "| **A task whose item is bold and much longer than the short_name cut** | robin "
            "| done 10:00 | 09:00 |  | Checklist: x |",
            started("A task whose item is bold and much longer than the short_name cut", "wt-unmerged"),
            sessions=session_row("robin"))
        self.addCleanup(tmp.cleanup)
        line = [l for l in al.audit(root, "2026-09-17").lines if l.startswith("reopen:")][0]
        self.assertIn("A task whose item is bold", line)
        self.assertNotIn("**", line)


class ShaClosesTheLaneTest(unittest.TestCase):
    """Finding 116(a). `reopen` asks whether the owner's *tree* is on main; `done` claims that *this
    task's change* reached main. A session working continuously in one worktree always has unmerged
    work, so every task it ever closed reopened — six of arch's did on the live tracker, and every
    one of their changes was already on main.

    A `done` state may name the commit that carried it: `done 21:16–22:05 54b7eb3`. Only positive
    proof clears a task. Absent, malformed, unknown to git, or git itself failing all fall back to
    exactly today's behaviour, which is noisy, self-clearing and visible — the property 116a's time
    heuristic did not have, and why it was discarded after this suite falsified it.
    """

    OWNS = started("Landed work", "wt-unmerged")
    DIRTY = started("Landed work", "wt-dirty")

    def tracker(self, state: str, log: str | None = None, sessions: str = "", owner: str = "robin"):
        tmp, root = workspace(f"| Landed work | {owner} | @STATE@ | 09:00 |  | Checklist: Landed work |",
                              log or self.OWNS, sessions)
        self.addCleanup(tmp.cleanup)
        repo = root / "repo"
        state = state.replace("@main@", git("rev-parse", "--short", "main", cwd=repo).strip())
        state = state.replace("@open@", git("rev-parse", "--short", "feat/open", cwd=repo).strip())
        p = root / "daily" / "2026-09-17-tracker.md"
        p.write_text(p.read_text().replace("@STATE@", state))
        return al.audit(root, "2026-09-17")

    def test_a_sha_on_main_closes_the_task_though_its_tree_is_not(self) -> None:
        """The whole point: the tree's other unmerged work is not evidence about this task."""
        report = self.tracker("done 10:00 @main@")
        self.assertEqual(report.reopen, [], report.lines)

    def test_a_sha_not_on_main_reopens_and_the_line_names_it(self) -> None:
        report = self.tracker("done 10:00 @open@")
        self.assertEqual(len(report.reopen), 1, report.lines)
        self.assertIn("is not on main", report.reopen[0].why)

    def test_a_malformed_sha_falls_back_and_says_so(self) -> None:
        report = self.tracker("done 10:00 not-a-sha")
        self.assertEqual(len(report.reopen), 1, report.lines)
        self.assertTrue(any("not-a-sha" in l for l in report.lines), report.lines)

    def test_a_sha_no_repo_knows_falls_back_and_says_so(self) -> None:
        """Stale evidence is not proof. A sha nothing can resolve must never clear a task."""
        report = self.tracker("done 10:00 deadbee")
        self.assertEqual(len(report.reopen), 1, report.lines)
        self.assertTrue(any("deadbee" in l for l in report.lines), report.lines)

    def test_a_closed_task_is_still_checked_for_orphaned_work(self) -> None:
        """This rule touches `reopen` and nothing else."""
        # `sam`, not the fixture's user: the user is exempt from the orphan join by design.
        report = self.tracker("done 10:00 @main@", log=self.DIRTY,
                              sessions=session_row("kim"), owner="sam")
        self.assertEqual(report.reopen, [], report.lines)
        self.assertEqual(len(report.orphans), 1, report.lines)


# the owner, 2026-09-22 22:20: each task "is actually backed by an entry in github". The audit is where the
# issue's state is read; `gh` is injected, so nothing here touches the network.
ISSUE_CLAUDE = CLAUDE + "- Backlog: GitHub issues; repo https://github.com/o/backlog (private)\n"

ISSUE_TRACKER = """# Tracker 2026-09-17

## Tasks

| name | item | owner | state | since | due | size | issue | checklist |
|---|---|---|---|---|---|---|---|---|
| Bare | Bare task | robin | open | 09:00 |  | S |  | c |
| Garbled | Garbled task | robin | open | 09:00 |  | S | TBD | c |
| Closed under it | Task whose issue closed | robin | waiting | 09:00 |  | S | #5 | c |
| Missing | Task whose issue is missing | robin | running 09:10 | 09:00 |  | S | #6 | c |
| Done open | Finished, issue still open | robin | done 09:00–10:00 | 09:00 |  | S | #7 | c |
| Backed | Backed task | robin | open | 09:00 |  | S | #8 | c |
| Old done | Closed before the rule | robin | done 08:00 | 08:00 |  | S |  | c |
| Elsewhere | Task in another repo | robin | open | 09:00 |  | S | x/y#2 | c |
| impl-3 — standing implementer | impl-3: standing implementer; wait idle | impl-3 | open | 09:00 |  |  |  |  |

## Log

- 09:00 opened the day
"""


class FakeGh:
    """`gh issue list --state all -R <repo>`, answered per repo. Records argv.

    `closed_at`, keyed the same way as `repos` (repo -> {number: iso timestamp}), answers #221's
    `closedAt`; a number with no entry there answers null, `gh`'s own word for an issue that never
    closed.
    """

    def __init__(self, repos: dict | None = None, fail: tuple | None = None, closed_at: dict | None = None):
        self.repos = repos or {}
        self.fail = fail
        self.closed_at = closed_at or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        assert args[:2] == ["issue", "list"], args
        if self.fail:
            return self.fail
        repo = args[args.index("-R") + 1]
        rows = [{"number": n, "title": "t", "state": st, "labels": [], "url": "", "updatedAt": "",
                 "closedAt": self.closed_at.get(repo, {}).get(n)}
                for n, st in self.repos.get(repo, {}).items()]
        return 0, json.dumps(rows), ""


LIVE = {"o/backlog": {5: "CLOSED", 7: "OPEN", 8: "OPEN"}, "x/y": {2: "OPEN"}}


def issue_workspace(claude: str = ISSUE_CLAUDE, tracker: str = ISSUE_TRACKER) -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "daily").mkdir()
    (root / "CLAUDE.md").write_text(claude)
    (root / "daily" / "2026-09-17-tracker.md").write_text(tracker)
    return tmp, root


# the owner, 2026-09-23 22:40: "remove the github issue remote and make everything use gitlab now that we have that going."
GITLAB_CLAUDE = CLAUDE + "- Backlog: GitLab; host https://gl.example; project o/backlog\n"


def gitlab_issues(case: unittest.TestCase):
    """`backlog.issues` patched so the GitLab Backlog's list is LIVE's `o/backlog`; a GitHub repo is still `gh`'s."""
    import backlog as bl
    real = bl.issues

    def fake(cfg, state="opened", **kw):
        if isinstance(cfg, bl.Backlog):
            case.assertEqual((cfg.host, cfg.project, state), ("https://gl.example", "o/backlog", "all"))
            return bl.Fetch(issues=tuple(bl.Issue(n, "t", bl.CLOSED if s == "CLOSED" else bl.OPEN)
                                         for n, s in LIVE["o/backlog"].items()))
        return real(cfg, state=state, **kw)
    return patch.object(bl, "issues", fake)


class GitLabIssueAuditTest(unittest.TestCase):
    def test_a_gitlab_backlog_is_audited(self):
        tmp, root = issue_workspace(GITLAB_CLAUDE)
        self.addCleanup(tmp.cleanup)
        with gitlab_issues(self):
            report = al.audit(root, "2026-09-17", gh=FakeGh(LIVE))
        self.assertEqual([str(f) for f in report.issues], [
            "issue: Bare — no issue; file it with chief-of-stuff backlog --create",
            'issue: Garbled — "TBD" is not an issue reference',
            "issue: Closed under it — #5 is closed; write the task done",
            "issue: Missing — #6 not found in o/backlog",
            "issue: Done open — done but #7 is open; chief-of-stuff backlog --close 7 --commit",
        ])


class ForeignHostTest(unittest.TestCase):
    def test_a_cell_on_another_host_is_never_read(self):
        # R1 on PR 14: an issue cell of https://evil.example.com/a/b/-/issues/1 sent the Backlog's token to that host.
        import backlog as bl
        tmp, root = issue_workspace(GITLAB_CLAUDE)
        self.addCleanup(tmp.cleanup)
        tracker = root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text().replace("| x/y#2 |", "| https://evil.example.com/a/b/-/issues/1 |"))
        asked = []

        def fake(cfg, state="opened", **_kw):
            asked.append(getattr(cfg, "host", "github"))
            return bl.Fetch(issues=())
        with patch.object(bl, "issues", fake):
            report = al.audit(root, "2026-09-17", gh=FakeGh(LIVE))
        self.assertNotIn("https://evil.example.com", asked)
        self.assertIn('issue: Elsewhere — "https://evil.example.com/a/b/-/issues/1" is not an issue reference',
                      [str(f) for f in report.issues])


class SameHostProjectTest(unittest.TestCase):
    def test_another_project_on_the_backlog_host_is_read_on_that_host(self):
        import backlog as bl
        tmp, root = issue_workspace(GITLAB_CLAUDE)
        self.addCleanup(tmp.cleanup)
        tracker = root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text().replace("| x/y#2 |", "| https://gl.example/g/other/-/issues/2 |"))
        asked = []

        def fake(cfg, state="opened", **_kw):
            asked.append((type(cfg).__name__, getattr(cfg, "host", ""), getattr(cfg, "project", "")))
            return bl.Fetch(issues=(bl.Issue(2, "t", bl.OPEN),))
        with patch.object(bl, "issues", fake):
            al.audit(root, "2026-09-17", gh=FakeGh(LIVE))
        self.assertIn(("Backlog", "https://gl.example", "g/other"), asked)


class IssueAuditTest(unittest.TestCase):
    def setUp(self):
        tmp, self.root = issue_workspace()
        self.addCleanup(tmp.cleanup)

    def run_audit(self, gh, **kw):
        return al.audit(self.root, "2026-09-17", gh=gh, **kw)

    def test_each_finding(self):
        report = self.run_audit(FakeGh(LIVE))
        got = [str(f) for f in report.issues]
        self.assertEqual(got, [
            "issue: Bare — no issue; file it with chief-of-stuff backlog --create",
            'issue: Garbled — "TBD" is not an issue reference',
            "issue: Closed under it — #5 is closed; write the task done",
            "issue: Missing — #6 not found in o/backlog",
            "issue: Done open — done but #7 is open; chief-of-stuff backlog --close 7 --commit",
        ])
        for line in got:
            self.assertIn(line, report.lines)
        self.assertIn("issues=5", report.lines[-1])

    def test_one_call_per_repo(self):
        gh = FakeGh(LIVE)
        self.run_audit(gh)
        self.assertEqual(sorted(c[c.index("-R") + 1] for c in gh.calls), ["o/backlog", "x/y"])
        for call in gh.calls:
            self.assertEqual(call[call.index("--state") + 1], "all")

    def test_unknown_is_a_finding_never_a_pass(self):
        report = self.run_audit(FakeGh(fail=(1, "", "rate limited")))
        unknown = [str(f) for f in report.issues if str(f).startswith("issue: unknown — ")]
        self.assertEqual(len(unknown), 1, report.lines)
        self.assertIn("rate limited", unknown[0])
        self.assertNotIn("issues=0", report.lines[-1])

    def test_off(self):
        gh = FakeGh(LIVE)
        report = self.run_audit(gh, check_issues=False)
        self.assertEqual((gh.calls, report.issues), ([], []))
        self.assertIn("issues=off", report.lines[-1])

    def test_no_backlog_line_is_no_check(self):
        tmp, root = issue_workspace(CLAUDE)
        self.addCleanup(tmp.cleanup)
        gh = FakeGh(LIVE)
        report = al.audit(root, "2026-09-17", gh=gh)
        self.assertEqual((gh.calls, report.issues), ([], []))
        self.assertNotIn("issues=", report.lines[-1])

    def test_exit_code_counts_issue_findings(self):
        self.assertEqual(al.main(["--root", str(self.root), "--date", "2026-09-17"], gh=FakeGh(LIVE)), 5)

    def test_cli_off(self):
        gh = FakeGh(LIVE)
        self.assertEqual(al.main(["--root", str(self.root), "--date", "2026-09-17", "--no-issues"], gh=gh), 0)
        self.assertEqual(gh.calls, [])


class WorkflowStepAuditTest(unittest.TestCase):
    """#362, the user's rule: "you are NOT to file new issues for things that are part of the workflow (e.g. reviewing a mr)".
    An `issue` cell of exactly `workflow` marks a step the pipeline performs: no issue fault in any state. A blank cell and
    any other text that is not an issue still fault."""

    TRACKER = """# Tracker 2026-09-17

## Tasks

| name | item | owner | state | since | due | size | issue | checklist |
|---|---|---|---|---|---|---|---|---|
| Open step | Review the open merge request | robin | open | 09:00 |  | S | workflow | c |
| Waiting step | Review the second merge request | robin | waiting | 09:00 |  | S | workflow | c |
| Done step | Review the third merge request | robin | done 09:00–10:00 | 09:00 |  | S | workflow | c |
| Blank | Unfiled task | robin | open | 09:00 |  | S |  | c |
| Prose | Garbled task | robin | open | 09:00 |  | S | review it | c |

## Log

- 09:00 opened the day
"""

    def test_only_a_blank_or_a_non_issue_cell_faults(self):
        tmp, root = issue_workspace(tracker=self.TRACKER)
        self.addCleanup(tmp.cleanup)
        gh = FakeGh(LIVE)
        report = al.audit(root, "2026-09-17", gh=gh)
        self.assertEqual([str(f) for f in report.issues], [
            "issue: Blank — no issue; file it with chief-of-stuff backlog --create",
            'issue: Prose — "review it" is not an issue reference',
        ])
        self.assertEqual(gh.calls, [], "a workflow row is never looked up on the forge")

    def test_a_workflow_row_is_never_reported_in_any_state(self):
        # Open, waiting and done alike: no issue fault of any kind is reported for a workflow row.
        tmp, root = issue_workspace(tracker=self.TRACKER)
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17", gh=FakeGh(LIVE))
        text = "\n".join(report.lines)
        for name in ("Open step", "Waiting step", "Done step"):
            with self.subTest(name=name):
                self.assertNotIn(f"issue: {name}", text)


    def test_the_token_is_exactly_workflow_once_its_whitespace_is_stripped(self):
        # #366 review R1: only the exact word exempts. Any other casing, punctuation, plural or extra word is not an issue.
        table = "\n".join(f"| {n} | Item {n} | robin | open | 09:00 |  | S | {cell} | c |" for n, cell in (
            ("Padded", "  workflow  "), ("Capital", "Workflow"), ("Upper", "WORKFLOW"), ("Dotted", "workflow."),
            ("Plural", "workflows"), ("Phrase", "my workflow")))
        tracker = self.TRACKER.split("| Open step")[0] + table + "\n\n## Log\n\n- 09:00 opened the day\n"
        tmp, root = issue_workspace(tracker=tracker)
        self.addCleanup(tmp.cleanup)
        gh = FakeGh(LIVE)
        report = al.audit(root, "2026-09-17", gh=gh)
        self.assertEqual([str(f) for f in report.issues], [
            'issue: Capital — "Workflow" is not an issue reference',
            'issue: Upper — "WORKFLOW" is not an issue reference',
            'issue: Dotted — "workflow." is not an issue reference',
            'issue: Plural — "workflows" is not an issue reference',
            'issue: Phrase — "my workflow" is not an issue reference',
        ])
        self.assertEqual(gh.calls, [], "the padded workflow row is never looked up on the forge")


class IssueClosesAtLaneEndTest(unittest.TestCase):
    """A done row mid-lane is one stage of its issue's work; the issue closes when its card reaches the lane's end."""

    def test_only_a_row_done_at_the_last_stage_asks_to_close(self):
        tmp, root = issue_workspace(ISSUE_CLAUDE + "- Settings: `cos.toml`\n")
        self.addCleanup(tmp.cleanup)
        (root / "cos.toml").write_text(LANE_TOML)
        (root / "daily" / "2026-09-17-tracker.md").write_text(
            "# Tracker 2026-09-17\n\n## Tasks\n\n"
            "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| Review done | Review the change | a | done 09:00–10:00 | 09:00 | | S | build | review | #7 | c |\n"
            "| Merged | Merge the change | a | done 10:00–10:30 | 10:00 | | S | build | merge | #8 | c |\n\n## Log\n")
        report = al.audit(root, "2026-09-17", gh=FakeGh(LIVE))
        self.assertEqual([str(f) for f in report.issues],
                         ["issue: Merged — done but #8 is open; chief-of-stuff backlog --close 8 --commit"])


class ClosedIssueOnOpenTaskTest(unittest.TestCase):
    """#221: an issue closed on the forge (by a person, or a change's closing keyword) while its tracker
    task is still open/waiting/orphaned never moved the task before this — the audit only said
    "#N is closed" and stopped. The fault now carries its own fix, the way a done-but-open row already
    names `chief-of-stuff backlog --close N --commit`: write the task done when nothing is left to land,
    or hand the call to the user when the tree still holds work `main` never got.
    """

    TRACKER = """# Tracker 2026-09-17

## Tasks

| name | item | owner | state | since | due | size | issue | checklist |
|---|---|---|---|---|---|---|---|---|
| No tree work | No tree work, backed by an issue with no worktree | sam | waiting | 09:00 |  | S | #5 | c |
| Unlanded work | Unlanded work, its tree still ahead of main | robin | waiting | 09:00 |  | S | #6 | c |
| Landed work | Landed work, its tree already merged | kim | waiting | 09:00 |  | S | #7 | c |

## Log

- 09:00 opened the day
""" + started("Unlanded work, its tree still ahead of main", "wt-unmerged") + "\n" \
        + started("Landed work, its tree already merged", "wt-merged") + "\n"

    def workspace(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "daily").mkdir()
        (root / "CLAUDE.md").write_text(ISSUE_CLAUDE)
        (root / "daily" / "2026-09-17-tracker.md").write_text(self.TRACKER)
        build(root)
        self.addCleanup(tmp.cleanup)
        return root

    def test_no_worktree_writes_the_task_done_at_the_close_time(self):
        """Branch A, no tree at all: nothing can be unlanded, so the fix is to write the task done —
        at the time the forge closed the issue, converted into the workspace's own zone."""
        gh = FakeGh({"o/backlog": {5: "CLOSED", 6: "CLOSED", 7: "CLOSED"}},
                    closed_at={"o/backlog": {5: "2026-09-17T19:05:00Z"}})
        report = al.audit(self.workspace(), "2026-09-17", gh=gh)
        self.assertIn("issue: No tree work — #5 is closed 14:05; write the task done at 14:05",
                      [str(f) for f in report.issues])

    def test_a_landed_worktree_also_writes_the_task_done(self):
        """Branch A, a tree that IS there: `wt-merged`'s branch is already an ancestor of main, so it
        holds no work `main` is missing either, and the fix is the same as having no tree at all."""
        gh = FakeGh({"o/backlog": {5: "CLOSED", 6: "CLOSED", 7: "CLOSED"}},
                    closed_at={"o/backlog": {7: "2026-09-17T19:05:00Z"}})
        report = al.audit(self.workspace(), "2026-09-17", gh=gh)
        self.assertIn("issue: Landed work — #7 is closed 14:05; write the task done at 14:05",
                      [str(f) for f in report.issues])

    def test_no_time_is_named_when_the_forge_gave_none(self):
        """An empty `closed_at` omits the clock rather than printing a false one."""
        gh = FakeGh({"o/backlog": {5: "CLOSED", 6: "CLOSED", 7: "CLOSED"}})
        report = al.audit(self.workspace(), "2026-09-17", gh=gh)
        self.assertIn("issue: No tree work — #5 is closed; write the task done",
                      [str(f) for f in report.issues])

    def test_an_unlanded_worktree_asks_the_user_instead(self):
        """Branch B: `wt-unmerged` carries a commit `main` never got — the same signal `reopen` already
        reads off a done task's tree — so the fault refuses to guess and hands the call to the user.
        No issue is ever reopened over this: the fix names a choice, not an action taken."""
        gh = FakeGh({"o/backlog": {5: "CLOSED", 6: "CLOSED", 7: "CLOSED"}})
        report = al.audit(self.workspace(), "2026-09-17", gh=gh)
        self.assertIn(
            "issue: Unlanded work — #6 is closed but its tree has work not on main; "
            "ask the user: reopen the issue or drop the work",
            [str(f) for f in report.issues])

    def unreadable_fault(self, root: Path, patched=None) -> str:
        gh = FakeGh({"o/backlog": {5: "CLOSED", 6: "CLOSED", 7: "CLOSED"}})
        with patched or contextlib.nullcontext():
            report = al.audit(root, "2026-09-17", gh=gh)
        [fault] = [str(f) for f in report.issues if "#6" in str(f)]
        return fault

    def assert_asks_the_user_it_is_unknown(self, fault: str) -> None:
        """V1: git could not read the tree, so the audit knows neither that work is unlanded nor that nothing is. It asks
        the user, in the audit's one wording for "git could not say", and says neither of the two things it does not know."""
        self.assertIn("Unlanded work — #6 is closed", fault)
        self.assertIn(unreadable_phrase(), fault)
        self.assertIn("ask the user", fault)
        self.assertNotIn("write the task done", fault)
        self.assertNotIn("work not on main", fault)
        self.assertNotIn("reopen the issue or drop the work", fault)

    def test_a_tree_git_cannot_read_is_not_work_not_on_main(self):
        """R7 + V1: `_tree_unlanded` matched the text "not on main", which an unreadable tree (128, not git's `no`) also printed, so a
        closed issue told the user to reopen it or drop work nobody had seen; once that text went, the gate answered "not unlanded"
        and the same issue was told to write the task done. Neither: it asks the user, saying git could not read the tree.
        Exit 1 stays the real `no` (the test above)."""
        for label, patched in (("a tree the view refuses", None), ("a merge-base git cannot answer", refusing("merge-base"))):
            with self.subTest(label):
                root = self.workspace()
                if patched is None:
                    refuse(root / "trees" / "wt-unmerged")
                self.assert_asks_the_user_it_is_unknown(self.unreadable_fault(root, patched))

    def test_the_unlanded_gate_does_not_read_the_prose_of_the_tree_state(self):
        """V1: the gate searched the wording `_state` prints. Reword whatever string `_state` reports and each of the three
        trees still gets its own answer: unmerged asks the user about unlanded work, landed writes the task done, unreadable asks
        the user with the unknown wording."""
        real = al._state

        def reworded(*args, **kw):
            result = real(*args, **kw)
            if isinstance(result, tuple):
                return tuple(re.sub(r"not on main|on main", "elsewhere", x) if isinstance(x, str) else x for x in result)
            return result

        gh = FakeGh({"o/backlog": {5: "CLOSED", 6: "CLOSED", 7: "CLOSED"}})
        with patch.object(al, "_state", reworded):
            readable = [str(f) for f in al.audit(self.workspace(), "2026-09-17", gh=gh).issues]
            unreadable_root = self.workspace()
            refuse(unreadable_root / "trees" / "wt-unmerged")
            unreadable = self.unreadable_fault(unreadable_root, patch.object(al, "_state", reworded))
        self.assertIn("issue: Unlanded work — #6 is closed but its tree has work not on main; "
                      "ask the user: reopen the issue or drop the work", readable)
        self.assertIn("issue: Landed work — #7 is closed; write the task done", readable)
        self.assert_asks_the_user_it_is_unknown(unreadable)

    def test_an_unlanded_worktree_keyed_on_the_tasks_name_asks_the_user_too(self):
        """#51: a prefix-less item whose started line is keyed on the task's `name` — `_tree_unlanded`
        must hand the name to `rows_for`, or this task is attributed no tree and is told to write it done."""
        root = self.workspace()
        tracker = root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text().replace(
            "\n## Log", "| alpha | Tidy the importer | sam | waiting | 09:00 |  | S | #8 | c |\n\n## Log", 1
        ) + started("alpha", "wt-unmerged") + "\n")
        gh = FakeGh({"o/backlog": {5: "CLOSED", 6: "CLOSED", 7: "CLOSED", 8: "CLOSED"}})
        report = al.audit(root, "2026-09-17", gh=gh)
        self.assertIn(
            "issue: alpha — #8 is closed but its tree has work not on main; "
            "ask the user: reopen the issue or drop the work",
            [str(f) for f in report.issues])


LANE_CLAUDE = CLAUDE + "- Settings: `cos.toml`\n"
LANE_TOML = '[lanes]\nbuild = { stages = ["implement", "pr", "review", "triage", "merge"], gates = ["triage", "merge"] }\n'
LANE_TRACKER = TRACKER.replace(
    "| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|\n{rows}",
    "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| Good | Good one | a | running 10:00 | 10:00 | | M | build | review | | c |\n"
    "| Unnamed lane | x | a | open | 10:00 | | M | designed | design | | c |\n"
    "| Wrong stage | x | a | open | 10:00 | | M | build | design | | c |\n"
    "| Blank stage | x | a | open | 10:00 | | M | build | | | c |\n"
    "| No lane | a console action | a | open | 10:00 | | S | | | | c |").format(rows="", sessions="", log="")


class LaneAuditTest(unittest.TestCase):
    """#33: a task's `lane` is one the toml names, and its `stage` is a stage of that lane."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "daily").mkdir()
        (self.root / "CLAUDE.md").write_text(LANE_CLAUDE)
        (self.root / "cos.toml").write_text(LANE_TOML)
        (self.root / "daily" / "2026-09-17-tracker.md").write_text(LANE_TRACKER)

    def test_each_finding(self):
        report = al.audit(self.root, "2026-09-17")
        got = [str(f) for f in report.lanes]
        self.assertEqual(got, [
            "lane: Unnamed lane — lane designed is not in cos.toml",
            "lane: Wrong stage — stage design is not a stage of build",
            "lane: Blank stage — lane build but no stage",
        ])
        for line in got:
            self.assertIn(line, report.lines)
        self.assertIn(" lanes=3", report.lines[-1])

    def test_the_exit_counts_them(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(al.main(["--root", str(self.root), "--date", "2026-09-17"]), 3)

    def test_no_lane_column_says_nothing(self):
        (self.root / "daily" / "2026-09-17-tracker.md").write_text(TRACKER.format(rows="| x | a | open | 10:00 | | c |", sessions="", log=""))
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual(report.lanes, [])
        self.assertNotIn("lanes=", report.lines[-1])


# #363: BUILD draws a card only for a row with a lane AND a stage (render_board.build_columns:
# `laned = [t for t in tasks if t.lane.strip() and t.stage.strip()]`). A stage with no lane vanished silently,
# so the audit faults it: for every state, and whether or not the settings name any lanes.
LANELESS_TRACKER = TRACKER.replace(
    "| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|\n{rows}",
    "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| Drops out | x | a | open | 10:00 | | M | | review | | c |\n"
    "| Decision | a console action | a | open | 10:00 | | S | | | | c |\n"
    "| Step | a review | a | open | 10:00 | | S | | | workflow | c |\n"
    "| Finished | done earlier | a | done 09:00-10:00 | 09:00 | | S | | | | c |\n"
    "| impl02 | impl02: standing implementer. wait idle | impl02 | waiting | 09:00 | | | | | | c |").format(rows="", sessions="", log="")


class StageWithNoLaneTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "daily").mkdir()
        (self.root / "CLAUDE.md").write_text(LANE_CLAUDE)
        (self.root / "cos.toml").write_text(LANE_TOML)
        self.tracker = self.root / "daily" / "2026-09-17-tracker.md"
        self.tracker.write_text(LANELESS_TRACKER)

    def test_a_stage_with_no_lane_is_one_named_fault_and_the_lane_less_rows_are_not(self):
        report = al.audit(self.root, "2026-09-17")
        got = [str(f) for f in report.lanes]
        self.assertEqual(got, ["lane: Drops out \u2014 stage review but no lane"])
        self.assertIn(got[0], report.lines)
        self.assertIn(" lanes=1", report.lines[-1])

    def test_the_exit_counts_it(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(al.main(["--root", str(self.root), "--date", "2026-09-17"]), 1)

    def test_a_done_row_with_a_stage_and_no_lane_faults_too(self):
        """A done row is still drawn in BUILD, so one that loses its card is a real drop."""
        self.tracker.write_text(LANELESS_TRACKER.replace(
            "| Finished | done earlier | a | done 09:00-10:00 | 09:00 | | S | | | | c |",
            "| Finished | done earlier | a | done 09:00-10:00 | 09:00 | | S | | merge | | c |"))
        got = [str(f) for f in al.audit(self.root, "2026-09-17").lanes]
        self.assertIn("lane: Finished \u2014 stage merge but no lane", got)

    def test_a_stage_with_no_lane_faults_with_no_lanes_table_and_no_settings_file(self):
        """The card is undrawn whatever the settings say, so the fault does not wait on a `[lanes]` table."""
        (self.root / "cos.toml").unlink()
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual([str(f) for f in report.lanes], ["lane: Drops out \u2014 stage review but no lane"])


BUDGET_TOML = '[budgets]\nS = "30m"\nM = "90m"\nL = "3h"\n'
BUDGET_TRACKER = TRACKER.replace(
    "| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|\n{rows}",
    "| name | item | owner | state | since | due | size | checklist |\n|---|---|---|---|---|---|---|---|\n"
    "| Long M | x | a | running 10:00 | 10:00 | | M | c |\n"
    "| Short M | x | a | running 11:00 | 11:00 | | M | c |\n"
    "| Long S | x | a | running 11:30 | 11:30 | | S | c |\n"
    "| At S | x | a | running 11:40 | 11:40 | | S | c |\n"
    "| Huge XL | x | a | running 06:00 | 06:00 | | XL | c |\n"
    "| Unsized | x | a | running 06:00 | 06:00 | | | c |\n"
    "| Open L | x | a | open | 06:00 | | L | c |").format(rows="", sessions="", log="")


class BudgetAuditTest(unittest.TestCase):
    """#33 stage 3: a running task past its size's budget is named; the coordinator polls it and never kills it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "daily").mkdir()
        (self.root / "CLAUDE.md").write_text(LANE_CLAUDE)
        (self.root / "cos.toml").write_text(BUDGET_TOML)
        (self.root / "daily" / "2026-09-17-tracker.md").write_text(BUDGET_TRACKER)

    def test_over_and_under(self):
        now = datetime(2026, 9, 17, 12, 10, tzinfo=ZoneInfo("America/Chicago"))
        report = al.audit(self.root, "2026-09-17", now=now)
        got = [str(o) for o in report.over]
        self.assertEqual(got, ["over budget: Long M running 2h10m (M 1h30m)", "over budget: Long S running 40m (S 30m)"])
        for line in got:
            self.assertIn(line, report.lines)
        self.assertIn(" over=2", report.lines[-1])

    def test_no_budgets_says_nothing(self):
        (self.root / "cos.toml").write_text("")
        report = al.audit(self.root, "2026-09-17", now=datetime(2026, 9, 17, 23, 0, tzinfo=ZoneInfo("America/Chicago")))
        self.assertEqual(report.over, [])
        self.assertNotIn("over=", report.lines[-1])




# #45: "When a task's item names a merge request or pull request that has merged, the task still reads as
# stalled." The rule: a merged change on a task that isn't done is a `merged:` finding that says how to clear
# it: write the task done once the change is on main and green there, or write "merged" after the ref. The forge is
# injected (`gh` for GitHub, `call` for GitLab), so nothing here touches the network.
MERGE_SHA, OTHER_SHA = "c0ffee1" + "d" * 33, "b01dfac" + "e" * 33
FORBIDDEN_13 = {"type": "FORBIDDEN", "path": ["repository", "n13"], "message": "Resource not accessible"}
GL_URL, GH_URL = "https://gl.example/o/backlog/-/merge_requests/12", "https://github.com/o/backlog/pull/12"

CHANGE_TRACKER = ISSUE_TRACKER.split("| Bare |")[0] + "{rows}\n\n## Log\n\n- 09:00 opened the day\n"
STANDING_ROW = "| impl-3 — standing implementer | impl-3: standing implementer; wait idle | impl-3 | open | 09:00 |  |  |  | !12, PR #12 |"


def change_row(names: str, name: str = "Search", state: str = "running 09:10", issue: str = "#8",
               item: str = "Search pagination") -> str:
    """A task row whose checklist is `names`: what it says of its change (`!12`, `PR #12`, or the change's URL)."""
    return f"| {name} | {item} | robin | {state} | 09:00 |  | S | {issue} | {names} |"


def change_tracker(*rows: str) -> str:
    return CHANGE_TRACKER.format(rows="\n".join(rows))


def home(claude: str):
    return al.parse_coordinator(claude, today=datetime.now().date()).backlog


def printed(ref: str) -> str:
    """A change's ref as a row spells it: `!12` on GitLab, `PR #12` on GitHub, where a bare `#12` is an issue."""
    return f"PR {ref}" if ref.startswith("#") else ref


def change(ref: str, state: str = "merged", base: str = "release", sha: str = MERGE_SHA):
    """A change as `board_sources.change_states` answers it; only a merged one carries a merge sha."""
    return bs.ChangeState(ref, state, base, sha if state == "merged" else "")


class MergedChangeRuleTest(unittest.TestCase):
    """`merged_faults(tasks, changes, home, answered=True)`: one fault per live task (not standing, not done, its
    issue not in another repo) for the changes its row names and does not record as merged, unless one is open."""

    def faults(self, claude: str, *rows: str, changes: dict, **kw) -> list:
        return al.merged_faults(al.parse_tracker(change_tracker(*rows)).tasks, changes, home(claude), **kw)

    def test_an_unacknowledged_merged_change_on_a_live_task_is_one_fault(self):
        """The fault lists "each merged one as `<ref> merged into <base> <sha7>`", joined by `, `. On GitHub
        "the fault prints the ref the way a row spells it, `PR #277 merged into main 294cb30`"; the `changes`
        keys stay `#N` / `!N`."""
        gitlab, github = {"!12": change("!12")}, {"#12": change("#12")}
        both = {**gitlab, "!10": change("!10", sha=OTHER_SHA)}
        pulls = {**github, "#13": change("#13", sha=OTHER_SHA)}
        cases = (
            ("GitLab !N", GITLAB_CLAUDE, change_row("!12"), gitlab, ["!12"]),
            ("GitLab change URL", GITLAB_CLAUDE, change_row(GL_URL, state="open"), gitlab, ["!12"]),
            ("GitHub PR #N", ISSUE_CLAUDE, change_row("PR #12", state="waiting"), github, ["#12"]),
            ("GitHub change URL", ISSUE_CLAUDE, change_row(GH_URL, state="orphaned"), github, ["#12"]),
            ("every merged ref", GITLAB_CLAUDE, change_row("!10 then !12"), both, ["!10", "!12"]),
            ("one acknowledged, one not", GITLAB_CLAUDE, change_row("!10 merged, then !12"), both, ["!12"]),
            # The issue cell is part of the row scan.
            ("a change URL in the issue cell", GITLAB_CLAUDE, change_row("c", issue=GL_URL), gitlab, ["!12"]),
            # Acknowledged is "the ref, then only non-word characters, then the word `merged`". These are not.
            ("waiting for the merge", GITLAB_CLAUDE, change_row("waiting for !12 to be merged"), gitlab, ["!12"]),
            ("a bare mention", ISSUE_CLAUDE, change_row("Fix the regression PR #12 introduced"), github, ["#12"]),
            ("a longer word", GITLAB_CLAUDE, change_row("!12 mergedx"), gitlab, ["!12"]),
            # "only `PR #277 merged` or the URL form acknowledges"
            ("the bare number says merged", ISSUE_CLAUDE, change_row("#12 merged, held", item="Search pagination, PR #12"), github, ["#12"]),
            # "The second form still names #9001 (we do not parse prose after the ref)."
            ("another repo named after the ref", ISSUE_CLAUDE, change_row("upstream PR #12 in cli/cli"), github, ["#12"]),
            # "a loud wrong finding that the reader can clear beats a silent miss": whatever comes before `PR`,
            # the row names the home repo's #12, because "the audit cannot tell prose from a repo name".
            ("another repo named before the ref", ISSUE_CLAUDE, change_row("blocked on upstream cli/cli PR #12"), github, ["#12"]),
            ("a remote branch ends the sentence before", ISSUE_CLAUDE, change_row("Rebased onto origin/main. PR #12 is up"), github, ["#12"]),
            ("a branch named before the ref", ISSUE_CLAUDE, change_row("pushed feat/search PR #12"), github, ["#12"]),
            ("the home URL, then another ref", ISSUE_CLAUDE, change_row(f"{GH_URL} PR #13"), pulls, ["#12", "#13"]),
            # "a ref and a following `merged` in the NEXT cell never combine"
            ("merged starts the next cell", GITLAB_CLAUDE,
             change_row("c", name="Search for !12", item="merged docs follow"), gitlab, ["!12"]),
            ("merged starts the cell after an empty one", GITLAB_CLAUDE,
             change_row("merged docs follow", item="Search waits on !12", issue=""), gitlab, ["!12"]),
            # "Repo and host compared case-insensitively"; "No issue cell, or an unparseable one, is live."
            ("its issue names the home repo in another case", ISSUE_CLAUDE, change_row("PR #12", issue="O/Backlog#8"), github, ["#12"]),
            ("it has no issue", ISSUE_CLAUDE, change_row("PR #12", issue=""), github, ["#12"]),
            ("its issue cell is not a reference", ISSUE_CLAUDE, change_row("PR #12", issue="TBD"), github, ["#12"]),
        )
        for what, claude, row, changes, refs in cases:
            with self.subTest(what):
                faults = self.faults(claude, row, changes=changes)
                self.assertEqual(len(faults), 1, faults)
                line = str(faults[0])
                self.assertTrue(line.startswith("merged: Search"), line)
                said = line.split(" — ", 1)[1]
                for ref, c in changes.items():
                    if ref in refs:
                        # Seven characters of the sha, and no more, end what is said of each change.
                        self.assertRegex(said, rf"{re.escape(printed(ref))} merged into {c.base} {c.merge_sha[:7]}(?=[,;]|$)")
                    else:
                        self.assertNotIn(ref, said)
                # "a merged line tells the reader both ways to clear it": the task is done only once the change
                # is on main and green there, or the row records the merge by writing "merged" after the ref.
                _, cut, how = said.partition("; ")
                self.assertTrue(cut, line)
                for word in (r"\bdone\b", r"\bmain\b", '"merged"', r"\bafter\b"):
                    self.assertRegex(how, word)

    def test_the_refs_in_a_line_are_in_ascending_number_order(self):
        """"Refs in a line are in ascending number order", however the row orders them."""
        changes = {ref: change(ref) for ref in ("!9", "!10", "!12")}
        line = str(self.faults(GITLAB_CLAUDE, change_row("!12 after !9, with !10"), changes=changes)[0])
        self.assertEqual(re.findall(r"!\d+", line), ["!9", "!10", "!12"])

    def test_a_merge_with_no_sha_leaves_no_gap_in_the_line(self):
        """A merged ref reads `<ref> merged into <base> <sha7>`, with "no gap when the sha is empty"."""
        line = str(self.faults(GITLAB_CLAUDE, change_row("!12"), changes={"!12": change("!12", sha="")})[0])
        self.assertIn("!12 merged into release", line)
        for gap in ("  ", " ;"):
            self.assertNotIn(gap, line)

    def test_a_named_change_the_forge_does_not_have_is_not_found(self):
        """The fault lists, "only when `answered`, each missing one as `<ref> not found`", beside the merged
        ones; "a line with only not-found refs has no instruction clause". On GitHub "not-found reads
        `PR #999 not found`"."""
        merged = {"!12": change("!12")}
        self.assertEqual([str(f) for f in self.faults(GITLAB_CLAUDE, change_row("!12"), changes={})],
                         ["merged: Search — !12 not found"])
        self.assertEqual([str(f) for f in self.faults(ISSUE_CLAUDE, change_row("PR #999"), changes={})],
                         ["merged: Search — PR #999 not found"])
        for answered, found in ((True, True), (False, False)):
            with self.subTest("beside a merged one", answered=answered):
                faults = self.faults(GITLAB_CLAUDE, change_row("!12 then !99"), changes=merged, answered=answered)
                self.assertEqual(len(faults), 1, faults)
                for part in ("!12 merged into release", MERGE_SHA[:7]):
                    self.assertIn(part, str(faults[0]))
                self.assertEqual("!99 not found" in str(faults[0]), found, faults)
        # A forge that answered with an error may only have left the change out.
        self.assertEqual(self.faults(GITLAB_CLAUDE, change_row("!12"), changes={}, answered=False), [])

    def test_an_open_change_clears_only_the_task_that_names_it(self):
        """"Unless one is open" is per task: another task's merged change is still its own finding."""
        faults = self.faults(GITLAB_CLAUDE, change_row("!12"), change_row("!13", name="Export"),
                             changes={"!12": change("!12", "open"), "!13": change("!13")})
        self.assertEqual([f.task for f in faults], ["Export"])

    def test_no_fault(self):
        gitlab, github = {"!12": change("!12")}, {"#12": change("#12")}
        opened = {"!12": change("!12", "open")}
        cases = (
            ("its change is closed", GITLAB_CLAUDE, change_row("!12"), {"!12": change("!12", "closed")}),
            # "Any open unacknowledged named change: no fault."
            ("its change is open", GITLAB_CLAUDE, change_row("!12"), opened),
            ("a later change is still open", GITLAB_CLAUDE, change_row("!10 then !12"), {**opened, "!10": change("!10")}),
            ("one is open and one is missing", GITLAB_CLAUDE, change_row("!12 then !99"), opened),
            # "Live task: not standing, not done, issue not in another repo."
            ("the task is done", GITLAB_CLAUDE, change_row("!12", state="done 09:00–10:00"), gitlab),
            ("the row is a standing session's", GITLAB_CLAUDE, STANDING_ROW, gitlab),
            ("its issue is in another repo", ISSUE_CLAUDE, change_row("PR #12", issue="x/y#2"), github),
            ("its issue is in another project on the host", GITLAB_CLAUDE,
             change_row("!12", issue="https://gl.example/g/other/-/issues/2"), gitlab),
            # "Issue not in another repo, host compared": on a GitLab home the short form reads as GitHub's o/backlog.
            ("its issue is the home project's name on another host", GITLAB_CLAUDE, change_row("!12", issue="o/backlog#8"), gitlab),
            # "Acknowledged = the ref, then only non-word characters, then the word `merged` ... case-insensitive"
            ("!12 merged", GITLAB_CLAUDE, change_row("!12 merged, held for the release note"), gitlab),
            ("!12, merged", GITLAB_CLAUDE, change_row("!12, merged"), gitlab),
            ("**!12** merged", GITLAB_CLAUDE, change_row("**!12** merged"), gitlab),
            ("`!12` merged", GITLAB_CLAUDE, change_row("`!12` merged"), gitlab),
            ("!12 MERGED", GITLAB_CLAUDE, change_row("!12 MERGED"), gitlab),
            ("<url> merged", GITLAB_CLAUDE, change_row(f"{GL_URL} merged"), gitlab),
            ("PR #12 (merged)", ISSUE_CLAUDE, change_row("PR #12 (merged)"), github),
            ("PR #12: merged", ISSUE_CLAUDE, change_row("PR #12: Merged; deploy pending"), github),
            ("[PR #12](<pull url>) merged", ISSUE_CLAUDE, change_row(f"[PR #12]({GH_URL}) merged"), github),
            ("<pull url> merged", ISSUE_CLAUDE, change_row(f"{GH_URL} merged"), github),
            ("an acknowledged change the forge does not have", GITLAB_CLAUDE, change_row("!12 merged"), {}),
        )
        for what, claude, row, changes in cases:
            with self.subTest(what):
                self.assertEqual(self.faults(claude, row, changes=changes), [])


def gl_change(iid: int, state: str = "merged") -> dict:
    return {**tbs.gl_mr(iid), "state": state, "mergeCommitSha": MERGE_SHA if state == "merged" else None}


class ForgeGh(FakeGh):
    """FakeGh for `issue list`, and test_board_sources' FakePulls for `api graphql`."""

    def __init__(self, repos: dict, *pulls: dict, **answer):
        super().__init__(repos)
        self.pulls = tbs.FakePulls(*pulls, **answer)

    def __call__(self, args, input_text=None):
        return self.pulls(args) if args[:2] == ["api", "graphql"] else super().__call__(args)


class MergedChangeAuditTest(unittest.TestCase):
    """#45: "`audit` reports that fault next to the existing issue faults." The wording is MergedChangeRuleTest's."""

    def audit(self, claude: str, *rows: str, gh=None, call=None, **kw):
        tmp, self.root = issue_workspace(claude, change_tracker(*rows))
        self.addCleanup(tmp.cleanup)
        with patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": "tok"}), gitlab_issues(self):
            return al.audit(self.root, "2026-09-17", gh=gh, call=call, **kw)

    def assert_merged(self, report, tasks: list[str]) -> None:
        """The `merged:` lines printed are the report's findings, one per task named, counted in the summary."""
        self.assertEqual([f.task for f in report.merged], tasks, report.lines)
        self.assertEqual([line for line in report.lines if line.startswith("merged:")], [str(f) for f in report.merged])
        self.assertIn(f" merged={len(tasks)}", report.lines[-1])

    def test_an_in_progress_task_whose_merge_request_merged_is_flagged_with_the_sha(self):
        """#45's acceptance: "a mocked GitLab: an in-progress task names `!12`, and `!12` has merged. `audit`
        flags it with the merge sha." The adapter is "configured from the existing Backlog host, project and
        token", and asks once."""
        call = tbs.StrictGitLab(mrs=[gl_change(12)])
        report = self.audit(GITLAB_CLAUDE, change_row("!12"), call=call)
        self.assertEqual(call.posts, [("POST", "https://gl.example/api/graphql", "tok")])
        self.assertIn('"o/backlog"', call.queries[0])
        self.assert_merged(report, ["Search"])
        self.assertIn(MERGE_SHA[:7], str(report.merged[0]))

    def test_a_merge_request_the_forge_does_not_have_is_a_finding(self):
        """GitLab: "a missing iid is simply absent from nodes (no error)", so the row's `!12` is not found."""
        report = self.audit(GITLAB_CLAUDE, change_row("!12"), call=tbs.StrictGitLab())
        self.assert_merged(report, ["Search"])

    def test_a_merged_pull_request_and_a_closed_issue_are_both_findings(self):
        """GitHub's `PR #N`. "A task with a closed issue and a merged change simply gets both findings.\""""
        gh = ForgeGh(LIVE, tbs.gh_pull(12), tbs.gh_pull(13))
        report = self.audit(ISSUE_CLAUDE, change_row("PR #12"),
                            change_row("PR #13", name="Closed one", state="waiting", issue="#5"), gh=gh)
        self.assertEqual(len(gh.pulls.calls), 1)
        self.assertEqual([f.task for f in report.issues], ["Closed one"])
        self.assert_merged(report, ["Search", "Closed one"])

    def test_a_number_that_is_not_a_pull_request_is_not_found(self):
        """GitHub answers such a number with a NOT_FOUND error and a failing `gh`: that is `<ref> not found`
        on the task's line, beside any merged change, and never `merged: unknown`."""
        report = self.audit(ISSUE_CLAUDE, change_row("PR #999"), gh=ForgeGh(LIVE))
        self.assert_merged(report, ["Search"])
        self.assertEqual(str(report.merged[0]), "merged: Search — PR #999 not found")
        report = self.audit(ISSUE_CLAUDE, change_row("PR #12, PR #999"), gh=ForgeGh(LIVE, tbs.gh_pull(12)))
        self.assert_merged(report, ["Search"])
        for part in ("PR #12 merged into main", "PR #999 not found"):
            self.assertIn(part, str(report.merged[0]))

    def test_the_forge_is_asked_only_for_the_changes_live_rows_leave_unacknowledged(self):
        """`change_states` is called "once with exactly those numbers (not done rows', not other-repo rows')"."""
        gh = ForgeGh(LIVE, *(tbs.gh_pull(n) for n in (12, 13, 14, 15)))
        report = self.audit(ISSUE_CLAUDE, change_row("PR #12"),
                            change_row("PR #13", name="Finished", state="done 09:00–10:00", issue=""),
                            change_row("PR #14", name="Elsewhere", issue="x/y#2"),
                            change_row("PR #15 merged, held for the release", name="Held"), gh=gh)
        self.assertEqual((len(gh.pulls.calls), gh.pulls.asked), (1, {12}))
        self.assert_merged(report, ["Search"])

    def test_a_mistyped_issue_cell_elsewhere_is_not_a_forge_error(self):
        """The lean read "never asks for issues": an issue number the forge does not have is `issue_faults`' to
        report, and a `gh` that would answer NOT_FOUND for it is never asked."""
        gh = ForgeGh(LIVE, tbs.gh_pull(12))
        report = self.audit(ISSUE_CLAUDE, change_row("PR #12"), change_row("c", name="Typo", issue="#404"), gh=gh)
        self.assertEqual([f.task for f in report.issues], ["Typo"])
        self.assertEqual(gh.pulls.asked, {12})
        self.assert_merged(report, ["Search"])

    def test_no_live_row_naming_an_unacknowledged_change_makes_no_forge_call(self):
        """The forge is read only when "some live row names an unacknowledged change", and the summary line
        gains ` merged=N` "only when the read happened"."""
        rows = (("no row names a change", change_row("c")),
                ("only a done row names one", change_row("!12, PR #12", state="done 09:00–10:00", issue="")),
                ("only a standing row names one", STANDING_ROW),
                ("the only one named is recorded as merged", change_row("!12 merged, PR #12 merged; held for the release")))
        cases = [(what, claude, row) for claude in (ISSUE_CLAUDE, GITLAB_CLAUDE) for what, row in rows]
        cases.append(("only a row whose issue is in another repo names one", ISSUE_CLAUDE, change_row("PR #12", issue="x/y#2")))
        # "Issue not in another repo, host compared": on a GitLab home the short form reads as GitHub's o/backlog.
        cases.append(("only a row whose issue is on another host names one", GITLAB_CLAUDE, change_row("!12", issue="o/backlog#8")))
        # A change URL of another repo, project or host is not a change the row names.
        cases.append(("the only URL is another repo's pull request", ISSUE_CLAUDE, change_row("https://github.com/cli/cli/pull/12")))
        cases.append(("the only URL is another project's merge request", GITLAB_CLAUDE,
                      change_row("https://gl.example/g/other/-/merge_requests/12")))
        cases.append(("the only URL is another host's merge request", GITLAB_CLAUDE,
                      change_row("https://other.example/o/backlog/-/merge_requests/12")))
        for what, claude, row in cases:
            with self.subTest(what, claude=claude.splitlines()[-1]):
                gh, call = ForgeGh(LIVE, tbs.gh_pull(12)), tbs.StrictGitLab(mrs=[gl_change(12)])
                report = self.audit(claude, row, gh=gh, call=call)
                self.assertEqual((gh.pulls.calls, call.posts, report.merged), ([], [], []))
                self.assertNotIn("merged", report.lines[-1])

    def test_issues_off_makes_no_forge_call(self):
        """The forge is read only when `cfg.backlog and check_issues`: `--no-issues` is the offline audit."""
        for claude, names in ((ISSUE_CLAUDE, "PR #12"), (GITLAB_CLAUDE, "!12")):
            with self.subTest(claude=claude.splitlines()[-1]):
                gh, call = ForgeGh(LIVE, tbs.gh_pull(12)), tbs.StrictGitLab(mrs=[gl_change(12)])
                report = self.audit(claude, change_row(names), gh=gh, call=call, check_issues=False)
                self.assertEqual((gh.calls, gh.pulls.calls, call.posts, report.merged), ([], [], [], []))
                self.assertNotIn("merged", report.lines[-1])

    def test_a_forge_that_cannot_be_read_is_one_unknown_finding(self):
        """"Error → the known faults plus one `merged: unknown — <err>`; a raise → one `merged: unknown`."
        A change missing from an answer that came with an error is not `not found`."""
        forbidden = {"errors": [{"type": "FORBIDDEN", "message": "Resource not accessible"}]}
        cases = (("the forge answers an error", "PR #12", {"fail": (1, "", "gh: not logged in")}, "not logged in", []),
                 ("the forge raises", "PR #12", {"fail": RuntimeError("the wire fell out")}, "the wire fell out", []),
                 ("part of the answer and an error", "PR #12", forbidden, "Resource not accessible", ["Search"]),
                 ("a missing number and an error", "PR #999", forbidden, "Resource not accessible", []),
                 # "Any other error is an error", at a requested pull request's own path too: only NOT_FOUND there is not.
                 ("a pull request the token may not read", "PR #12, PR #13", {"errors": [FORBIDDEN_13]}, "Resource not accessible", ["Search"]),
                 # NOT_FOUND on the repository itself is not `PR #12 not found`.
                 ("the repository is not there", "PR #12", {"missing_repo": True}, "Could not resolve to a Repository", []))
        for what, names, answer, err, tasks in cases:
            with self.subTest(what):
                report = self.audit(ISSUE_CLAUDE, change_row(names), gh=ForgeGh(LIVE, tbs.gh_pull(12), **answer))
                unknown = [str(f) for f in report.merged if str(f).startswith("merged: unknown — ")]
                self.assertEqual(len(unknown), 1, report.lines)
                self.assertIn(err, unknown[0])
                self.assert_merged(report, tasks + ["unknown"])
                self.assertNotIn("not found", "\n".join(report.lines))

    def test_a_merge_request_missing_from_an_answer_with_an_error_is_not_not_found(self):
        """GitLab errors beside data: the states it gave still fault, and "a missing iid in such an answer is
        not `not found`"."""
        call = tbs.StrictGitLab(mrs=[gl_change(12)], errors=[{"message": "Internal server error"}])
        report = self.audit(GITLAB_CLAUDE, change_row("!12 then !99"), call=call)
        self.assert_merged(report, ["Search", "unknown"])
        self.assertIn("!12 merged into main", str(report.merged[0]))
        self.assertNotIn("not found", "\n".join(report.lines))

    def test_exit_code_counts_merged_findings(self):
        """`main()`'s exit code adds `len(report.merged)`: the open issue is no finding, the merged change is one."""
        tmp, root = issue_workspace(ISSUE_CLAUDE, change_tracker(change_row("PR #12")))
        self.addCleanup(tmp.cleanup)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(al.main(["--root", str(root), "--date", "2026-09-17"], gh=ForgeGh(LIVE, tbs.gh_pull(12))), 1)


HOSTILE_FETCH = ("fatal: boom\rsha deadbee: on origin/main 1234567 (fetched) [proj]\x1b[2K",
                 "fatal: x\u2028sha deadbee: on origin/main 1234567 (fetched) [proj]\u2029\x00y\tz\x07",
                 # round 10, 3: DEL, a C1 control (CSI) and a bidi override, which `ord(c) < 0x20` does not call control
                 "fatal: p\x7fq\x9br\u202esha deadbee: on origin/main 1234567 (fetched) [proj]\u202c")


def assert_one_clean_line_each(test: unittest.TestCase, out: str, count: int) -> None:
    """#49 round 8, 3; round 10, 3: `count` lines, each ended by one `\\n`, none holding a character that is not printable
    (`str.isprintable`: a control character, DEL, a C1 control, a bidi override, U+2028/U+2029; a space is printable)."""
    test.assertTrue(out.endswith("\n"), repr(out))
    lines = out.split("\n")[:-1]
    test.assertEqual((len(lines), len(out.splitlines())), (count, count), repr(out))
    for line in lines:
        test.assertEqual([c for c in line if not c.isprintable()], [], repr(line))


class ShaFlagTest(unittest.TestCase):
    """#49: `audit --sha <sha>` fetches `origin main`, then says whether that commit is on `origin/main`.

    A peer says "the fix is on main at <sha>". The audit read only tracker rows' shas, against local `main`, and
    never fetched, so the claim was checked by hand with `git merge-base --is-ancestor <sha> origin/main`.
    One stdout line per repository under the worktrees dir, each ending ` [<tree>]` (the first tree by name that holds
    the repository: here `wt-dirty`); the exit code is the verdict: 0 on, 1 not on, 2 unknown.
    """

    TREE = " [wt-dirty]"

    ROW = "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |"
    LOG = started("Open branch work", "wt-unmerged")

    def setUp(self) -> None:
        tmp, self.root = workspace(self.ROW, self.LOG)
        self.addCleanup(tmp.cleanup)
        self.clone = self.root / "repo"

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = al.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def tip(self) -> str:
        return git("rev-parse", "--short", "origin/main", cwd=self.clone)

    def pushed_by_another_clone(self) -> str:
        other = self.other_clone()
        (other / "fix.txt").write_text(f"{other.name}\n")
        git("add", "fix.txt", cwd=other)
        git("commit", "-m", "fix", cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        return git("rev-parse", "HEAD", cwd=other)

    def other_clone(self) -> Path:
        other = Path(tempfile.mkdtemp(dir=self.root, prefix="other-"))
        git("clone", "-q", str(self.root / "origin.git"), str(other), cwd=self.root)
        git("config", "user.email", "o@example.test", cwd=other)
        git("config", "user.name", "Other", cwd=other)
        return other

    def test_a_commit_pushed_by_another_clone_is_found_by_fetching(self) -> None:
        """The issue's gap: the commit is on the remote's main and this clone has not fetched it."""
        new = self.pushed_by_another_clone()
        stale = self.tip()
        code, out, _ = self.run_main("--sha", new)
        self.assertNotEqual(self.tip(), stale)
        self.assertEqual(self.tip(), git("rev-parse", "--short", new, cwd=self.clone))
        self.assertEqual(out, f"sha {new}: on origin/main {self.tip()} (fetched){self.TREE}\n")
        self.assertEqual(code, 0)

    def test_an_abbreviated_sha_is_accepted(self) -> None:
        short = git("rev-parse", "--short", "origin/main", cwd=self.clone)
        code, out, _ = self.run_main("--sha", short)
        self.assertEqual((code, out), (0, f"sha {short}: on origin/main {self.tip()} (fetched){self.TREE}\n"))

    def test_a_commit_on_an_unmerged_branch_is_not_on_origin_main(self) -> None:
        sha = git("rev-parse", "feat/open", cwd=self.clone)
        code, out, _ = self.run_main("--sha", sha)
        self.assertEqual((code, out), (1, f"sha {sha}: not on origin/main {self.tip()} (fetched){self.TREE}\n"))

    def test_a_commit_on_local_main_not_pushed_is_said_so(self) -> None:
        """Local `main` is what the row audit asks; `--sha` asks the remote's, and says which main holds it (#49 G)."""
        (self.clone / "local.txt").write_text("local\n")
        git("add", "local.txt", cwd=self.clone)
        git("commit", "-m", "local only", cwd=self.clone)
        sha = git("rev-parse", "main", cwd=self.clone)
        code, out, _ = self.run_main("--sha", sha)
        self.assertEqual((code, out), (1, f"sha {sha}: on local main only, not pushed to origin/main {self.tip()} (fetched){self.TREE}\n"))

    def test_fetch_failed_still_says_local_main_holds_it(self) -> None:
        """#49 round 7, 5: unknown stays unknown (exit 2), and the fact that local main holds the commit is said."""
        (self.clone / "local.txt").write_text("local\n")
        git("add", "local.txt", cwd=self.clone)
        git("commit", "-m", "local only", cwd=self.clone)
        sha = git("rev-parse", "main", cwd=self.clone)
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        code, out, _ = self.run_main("--sha", sha)
        self.assertEqual(code, 2)
        self.assertEqual(out.count("\n"), 1)
        self.assertTrue(out.endswith(f"; origin/main {self.tip()} as last fetched does not hold it; local main holds it{self.TREE}\n"), out)

    def test_a_fetch_error_a_remote_controls_cannot_forge_or_split_a_line(self) -> None:
        """#49 round 8, 3a: `fetch_base` returns text with a carriage return, an escape, a line separator and a NUL, and
        a fake verdict line after the carriage return. One repository, one line, nothing raw; the escapes are shown."""
        # `fetch_base` is patched to return raw control characters, which the real one no longer does: this pins `main`'s own
        # escaping of whatever a verdict line carries, not `fetch_base`'s.
        sha = git("rev-parse", "origin/main", cwd=self.clone)
        for hostile in HOSTILE_FETCH:
            with self.subTest(hostile), patch.object(git_trees, "fetch_base", return_value=hostile):
                code, out, _ = self.run_main("--sha", sha)
                self.assertEqual(code, 2)
                assert_one_clean_line_each(self, out, 1)
                self.assertTrue(out.startswith(f"sha {sha}: unknown \u2014 fetch failed: fatal: "), repr(out))
        with patch.object(git_trees, "fetch_base", return_value=HOSTILE_FETCH[0]):
            out = self.run_main("--sha", sha)[1]
        self.assertIn("\\r", out)
        self.assertIn("\\x1b", out)

    def test_a_commit_nobody_has_is_no_such_commit_after_fetch(self) -> None:
        sha = "0123456789abcdef0123456789abcdef01234567"
        code, out, _ = self.run_main("--sha", sha)
        self.assertEqual((code, out), (1, f"sha {sha}: no such commit after fetch, so not on origin/main {self.tip()}{self.TREE}\n"))

    def test_fetch_failed_is_never_a_yes_even_when_the_last_fetched_ref_holds_it(self) -> None:
        """Main may have been rewritten since the last fetch, so what the stale ref holds is not an answer."""
        sha = git("rev-parse", "origin/main", cwd=self.clone)
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        code, out, _ = self.run_main("--sha", sha)
        self.assertEqual(code, 2)
        self.assertEqual(out.count("\n"), 1)
        self.assertTrue(out.startswith(f"sha {sha}: unknown \u2014 fetch failed: "), out)
        self.assertTrue(out.endswith(f"; origin/main {self.tip()} as last fetched holds it{self.TREE}\n"), out)
        self.assertTrue(out.split("fetch failed: ", 1)[1].split("; origin/main", 1)[0].strip(), out)

    def test_fetch_failed_and_the_last_fetched_ref_does_not_hold_it(self) -> None:
        """No answer, not a no: the remote may hold it. Exit 2, for a commit nobody has and for one on a branch."""
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        for what, sha in (("no such commit", "0123456789abcdef0123456789abcdef01234567"),
                          ("not an ancestor", git("rev-parse", "feat/open", cwd=self.clone))):
            with self.subTest(what):
                code, out, _ = self.run_main("--sha", sha)
                self.assertEqual(code, 2)
                self.assertEqual(out.count("\n"), 1)
                self.assertTrue(out.startswith(f"sha {sha}: unknown \u2014 fetch failed: "), out)
                self.assertTrue(out.endswith(f"; origin/main {self.tip()} as last fetched does not hold it{self.TREE}\n"), out)

    def test_a_ref_named_like_a_sha_is_not_a_commit(self) -> None:
        """The answer is about the commit whose id starts with the hex, never about a ref of that name."""
        main = git("rev-parse", "origin/main", cwd=self.clone)
        for what, make, name in (("tag", ("tag",), "deadbee"), ("branch", ("branch",), "badc0de")):
            with self.subTest(what):
                git(*make, name, main, cwd=self.clone)
                self.assertEqual(git("rev-parse", f"{name}^{{commit}}", cwd=self.clone), main)
                code, out, _ = self.run_main("--sha", name)
                self.assertEqual((code, out), (1, f"sha {name}: no such commit after fetch, so not on origin/main {self.tip()}{self.TREE}\n"))

    def test_a_ref_named_like_an_unmerged_commits_prefix_does_not_hide_it(self) -> None:
        unmerged = git("rev-parse", "feat/open", cwd=self.clone)
        short = unmerged[:7]
        git("tag", short, git("rev-parse", "origin/main", cwd=self.clone), cwd=self.clone)
        code, out, _ = self.run_main("--sha", short)
        self.assertEqual((code, out), (1, f"sha {short}: not on origin/main {self.tip()} (fetched){self.TREE}\n"))

    def test_uppercase_hex_and_a_leading_dash_are_usage_errors(self) -> None:
        main = git("rev-parse", "origin/main", cwd=self.clone)
        # `.upper()` changes nothing when the hex it is given has no a-f, which is a valid sha: cut the prefix at its first letter.
        first_letter = re.search("[a-f]", main)
        upper = main[:max(7, first_letter.end())].upper() if first_letter else "ABCDEF1"
        for bad in (main.upper() if first_letter else "ABCDEF1", upper, "-" + main[:7], "--" + main[:7]):
            with self.subTest(bad), patch.object(git_trees, "fetch_base", side_effect=AssertionError("fetched")):
                code, out, err = self.run_main(f"--sha={bad}")
                self.assertEqual((code, out, err), (2, "", "audit_tasks: --sha takes 7-64 lowercase hex digits\n"))

    def test_the_fetch_moves_origin_main_whatever_the_fetch_config_is(self) -> None:
        """`git fetch origin main` only updates `refs/remotes/origin/main` when `remote.origin.fetch` maps it."""
        for what, setup in (("no refspec", ("--unset-all", "remote.origin.fetch")),
                            ("maps only another branch", ("--replace-all", "remote.origin.fetch", "+refs/heads/other:refs/remotes/origin/other"))):
            with self.subTest(what):
                git("config", *setup, cwd=self.clone)
                new = self.pushed_by_another_clone()
                code, out, _ = self.run_main("--sha", new)
                self.assertEqual(git("rev-parse", "origin/main", cwd=self.clone), new)
                self.assertEqual((code, out), (0, f"sha {new}: on origin/main {self.tip()} (fetched){self.TREE}\n"))

    def test_a_commit_main_was_rewritten_without_is_not_on_origin_main(self) -> None:
        """Fetched once, then origin's main is force-pushed to a history without it: the fetch must follow,
        also when the clone's refspec is not forced (no leading `+`)."""
        for what, refspec in (("default refspec", None), ("unforced refspec", "refs/heads/*:refs/remotes/origin/*")):
            with self.subTest(what):
                if refspec:
                    git("config", "--replace-all", "remote.origin.fetch", refspec, cwd=self.clone)
                gone = self.pushed_by_another_clone()
                self.assertEqual(self.run_main("--sha", gone)[0], 0)
                rewriter = self.other_clone()
                git("reset", "--hard", "-q", "HEAD~1", cwd=rewriter)
                (rewriter / "other.txt").write_text(f"{rewriter.name}\n")
                git("add", "other.txt", cwd=rewriter)
                git("commit", "-m", "rewritten", cwd=rewriter)
                git("push", "-q", "--force", "origin", "main", cwd=rewriter)
                code, out, _ = self.run_main("--sha", gone)
                self.assertEqual((code, out), (1, f"sha {gone}: not on origin/main {self.tip()} (fetched){self.TREE}\n"))

    def test_no_origin_main_after_a_failed_fetch_is_unknown_without_a_path(self) -> None:
        git("update-ref", "-d", "refs/remotes/origin/main", cwd=self.clone)
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        sha = "0123456789abcdef"
        code, out, _ = self.run_main("--sha", sha)
        self.assertEqual(code, 2)
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(out, f"sha {sha}: unknown \u2014 no origin/main to check against{self.TREE}\n")
        for path in (self.clone, self.root / "trees"):
            self.assertNotIn(str(path), out)
            self.assertNotIn(str(path.resolve()), out)

    def test_a_malformed_sha_is_refused_on_stderr(self) -> None:
        """Not 7\u201364 hex, as `SHA` reads a done state's citation. Nothing is fetched for it."""
        for bad in ("zzz", "abc123", "0" * 65, "--help-me", "main", "54b7eb3;ls"):
            with self.subTest(bad), patch.object(git_trees, "fetch_base", side_effect=AssertionError("fetched")):
                code, out, err = self.run_main(f"--sha={bad}")
                self.assertEqual((code, out, err), (2, "", "audit_tasks: --sha takes 7-64 lowercase hex digits\n"))

    def test_a_64_digit_id_is_not_a_usage_error(self) -> None:
        """#49 round 11: 7-64 lowercase hex. 64 is a full SHA-256 object id; it reaches the fetch like any other id."""
        for sha in ("0123456789abcdef" * 4, "0" * 41):
            with self.subTest(len(sha)), patch.object(git_trees, "fetch_base", return_value="") as fetch:
                code, out, err = self.run_main(f"--sha={sha}")
                self.assertNotIn("takes 7-64", err)
                self.assertTrue(fetch.called)

    @unittest.skipUnless(tgt.git_makes_sha256(), "this git cannot `init --object-format=sha256`")
    def test_a_full_sha256_id_is_answered_through_main(self) -> None:
        """#49 round 11: the same as `check_sha`'s, through `--sha`: a SHA-256 origin, clone and worktree under the workspace."""
        for name in ("repo", "origin.git", "trees"):
            shutil.rmtree(self.root / name)
        origin, clone, wt = self.root / "origin.git", self.root / "repo", self.root / "trees" / "wt-dirty"
        origin.mkdir()
        clone.mkdir()
        git("init", "--bare", "--object-format=sha256", "--initial-branch=main", ".", cwd=origin)
        git("init", "--object-format=sha256", "--initial-branch=main", ".", cwd=clone)
        git("config", "user.email", "t@example.test", cwd=clone)
        git("config", "user.name", "Test", cwd=clone)
        (clone / "README.md").write_text("base\n")
        git("add", "README.md", cwd=clone)
        git("commit", "-m", "base", cwd=clone)
        git("remote", "add", "origin", str(origin), cwd=clone)
        git("push", "-u", "origin", "main", cwd=clone)
        git("worktree", "add", "-b", "feat/dirty", str(wt), "main", cwd=clone)
        sha = git("rev-parse", "HEAD", cwd=clone)
        self.assertEqual(len(sha), 64)
        code, out, err = self.run_main("--sha", sha)
        self.assertEqual((code, out, err), (0, f"sha {sha}: on origin/main {self.tip()} (fetched){self.TREE}\n", ""))

    def test_it_needs_no_tracker_for_the_day(self) -> None:
        (self.root / "daily" / "2026-09-17-tracker.md").unlink()
        sha = git("rev-parse", "origin/main", cwd=self.clone)
        code, out, err = self.run_main("--sha", sha)
        self.assertEqual((code, out, err), (0, f"sha {sha}: on origin/main {self.tip()} (fetched){self.TREE}\n", ""))

    def test_it_still_needs_the_config_root(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = al.main(["--root", d, "--sha", "0123456789abcdef"])
            self.assertEqual((code, out.getvalue()), (2, ""))
            self.assertIn("no CLAUDE.md", err.getvalue())

    def test_with_no_git_tree_there_is_nowhere_to_ask(self) -> None:
        """A different fault from a malformed sha, so a different line: it names where it looked as the config writes it
        (never an absolute path), or the workspace root when there is no `Worktrees:` line. Nothing is fetched."""
        for what, claude, where in (("a worktrees dir", CLAUDE, "trees/"),
                                    ("no worktrees line", CLAUDE.replace("- Worktrees: `trees/`\n", ""), "the workspace root")):
            with self.subTest(what), tempfile.TemporaryDirectory() as d, \
                    patch.object(git_trees, "fetch_base", side_effect=AssertionError("fetched")):
                (Path(d) / "CLAUDE.md").write_text(claude)
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = al.main(["--root", d, "--sha", "0123456789abcdef"])
                self.assertEqual((code, out.getvalue()), (2, ""))
                self.assertEqual(err.getvalue(), f"audit_tasks: --sha found no git tree under {where}, so nothing was checked\n")
                self.assertNotIn(d, err.getvalue())

    def test_a_plain_audit_never_fetches_and_reads_as_before(self) -> None:
        """Guard: the fetch belongs to `--sha`. The row audit stays offline and its lines and exit do not move."""
        before = self.run_main("--date", "2026-09-17")
        real, fetched = subprocess.run, []

        def watch(argv, *a, **kw):
            if "fetch" in argv:
                fetched.append(argv)
            return real(argv, *a, **kw)

        with patch.object(git_trees, "fetch_base", side_effect=AssertionError("fetched")), \
                patch.object(git_trees, "check_sha", side_effect=AssertionError("checked")), \
                patch.object(subprocess, "run", watch):
            during = self.run_main("--date", "2026-09-17")
        self.assertEqual(fetched, [])
        self.assertEqual(during, before)
        code, out, _ = before
        self.assertEqual(code, 1)
        self.assertIn("reopen: wt-unmerged (feat/open): not on main \u2014 1 done task: Open branch work", out)
        self.assertNotIn("sha ", out)

    # --- #49 round 6: wrong answers a review reproduced in real repos ---

    def test_only_the_remote_tracking_ref_is_read(self) -> None:
        """A. A branch, a tag or `refs/origin/main` named like it wins `rev-parse origin/main`, and an unmerged commit
        on it would come back `on origin/main`. The answer reads `refs/remotes/origin/main` and no other."""
        unmerged = git("rev-parse", "feat/open", cwd=self.clone)
        real_tip = git("rev-parse", "--short", "refs/remotes/origin/main", cwd=self.clone)
        for what, make, drop in (("a local branch", ("branch", "origin/main", unmerged), ("branch", "-D", "origin/main")),
                                 ("a tag", ("tag", "origin/main", unmerged), ("tag", "-d", "origin/main")),
                                 ("refs/origin/main", ("update-ref", "refs/origin/main", unmerged), ("update-ref", "-d", "refs/origin/main"))):
            with self.subTest(what):
                git(*make, cwd=self.clone)
                try:
                    code, out, _ = self.run_main("--sha", unmerged)
                finally:
                    git(*drop, cwd=self.clone)
                self.assertEqual((code, out), (1, f"sha {unmerged}: not on origin/main {real_tip} (fetched){self.TREE}\n"))

    def graft_origin_main_onto_the_unmerged_commit(self) -> tuple[str, str, str]:
        new = self.pushed_by_another_clone()
        git("fetch", "-q", "origin", cwd=self.clone)
        tip = git("rev-parse", "refs/remotes/origin/main", cwd=self.clone)
        self.assertEqual(tip, new)
        return tip, git("rev-parse", f"{tip}^", cwd=self.clone), git("rev-parse", "feat/open", cwd=self.clone)

    def test_a_replace_ref_cannot_make_a_yes(self) -> None:
        """B. `git replace --graft <tip> <parent> <unmerged>` makes the unmerged commit an ancestor of the tip for every
        reader that honours replace refs."""
        tip, parent, unmerged = self.graft_origin_main_onto_the_unmerged_commit()
        git("replace", "--graft", tip, parent, unmerged, cwd=self.clone)
        self.assertEqual(subprocess.run(["git", "merge-base", "--is-ancestor", unmerged, tip], cwd=self.clone).returncode, 0)
        code, out, _ = self.run_main("--sha", unmerged)
        self.assertEqual((code, out), (1, f"sha {unmerged}: not on origin/main {self.tip()} (fetched){self.TREE}\n"))

    def test_grafts_make_ancestry_untrustworthy(self) -> None:
        """B. A non-empty `info/grafts` in the common git dir (the tree asked is a worktree, so not its own `.git`):
        no yes and no no, for a commit on origin/main and for one that is not."""
        tip, parent, unmerged = self.graft_origin_main_onto_the_unmerged_commit()
        grafts = self.clone / ".git" / "info" / "grafts"
        grafts.parent.mkdir(exist_ok=True)
        grafts.write_text(f"{tip} {parent} {unmerged}\n")
        for what, sha in (("not on origin/main", unmerged), ("on origin/main", tip)):
            with self.subTest(what):
                code, out, _ = self.run_main("--sha", sha)
                self.assertEqual((code, out), (2, f"sha {sha}: unknown \u2014 this repository has grafts, so ancestry cannot be trusted{self.TREE}\n"))
        grafts.write_text("")  # an empty file grafts nothing
        code, out, _ = self.run_main("--sha", tip)
        self.assertEqual((code, out), (0, f"sha {tip}: on origin/main {self.tip()} (fetched){self.TREE}\n"))

    def force_push_main_without_the_last_commit(self) -> None:
        rewriter = self.other_clone()
        git("reset", "--hard", "-q", "HEAD~1", cwd=rewriter)
        (rewriter / "other.txt").write_text(f"{rewriter.name}\n")
        git("add", "other.txt", cwd=rewriter)
        git("commit", "-m", "rewritten", cwd=rewriter)
        git("push", "-q", "--force", "origin", "main", cwd=rewriter)

    def test_a_callers_git_dir_does_not_send_the_fetch_to_another_repository(self) -> None:
        """C. `GIT_DIR` beats `-C`: the fetch would update the decoy while the reads look at this clone's stale ref."""
        decoy = self.other_clone()
        gone = self.pushed_by_another_clone()
        self.assertEqual(self.run_main("--sha", gone)[0], 0)
        self.force_push_main_without_the_last_commit()
        with patch.dict(os.environ, {"GIT_DIR": str(decoy / ".git")}):
            code, out, _ = self.run_main("--sha", gone)
        self.assertEqual((code, out), (1, f"sha {gone}: not on origin/main {self.tip()} (fetched){self.TREE}\n"))

    def test_the_fetch_writes_no_tag_into_the_clone(self) -> None:
        """D. Upstream gains a tag on main; a fetch follows tags unless told not to."""
        other = self.other_clone()
        (other / "tagged.txt").write_text("x\n")
        git("add", "tagged.txt", cwd=other)
        git("commit", "-m", "tagged", cwd=other)
        git("tag", "upstream-tag", cwd=other)
        git("push", "-q", "origin", "main", "upstream-tag", cwd=other)
        new = git("rev-parse", "HEAD", cwd=other)
        code, out, _ = self.run_main("--sha", new)
        self.assertEqual(git("tag", "--list", cwd=self.clone), "")
        self.assertEqual((code, out), (0, f"sha {new}: on origin/main {self.tip()} (fetched){self.TREE}\n"))

    def test_a_flag_other_than_root_is_refused(self) -> None:
        """J. `--sha` is its own question: `--date` and `--no-issues` belong to the row audit and would be ignored."""
        sha = git("rev-parse", "origin/main", cwd=self.clone)
        for flags in (["--date", "2026-09-17"], ["--no-issues"]):
            with self.subTest(flags), patch.object(git_trees, "fetch_base", side_effect=AssertionError("fetched")):
                code, out, err = self.run_main("--sha", sha, *flags)
                self.assertEqual((code, out, err), (2, "", "audit_tasks: --sha takes no other flag but --root\n"))

    def test_a_sha_with_a_trailing_newline_is_refused(self) -> None:
        """`$` matches before a final newline; the usage check must not."""
        sha = git("rev-parse", "origin/main", cwd=self.clone)
        with patch.object(git_trees, "fetch_base", side_effect=AssertionError("fetched")):
            code, out, err = self.run_main(f"--sha={sha}\n")
        self.assertEqual((code, out, err), (2, "", "audit_tasks: --sha takes 7-64 lowercase hex digits\n"))


class ShaTreesTest(unittest.TestCase):
    """#49 E: every repository under the worktrees dir is asked, one line each, each ending ` [<tree>]`.

    The trees are what `git_trees.discover` returns, deduplicated by repository (trees sharing one `.git` are one
    repository; the first by name is asked and named); with none, the root itself if it is a git tree (` [.]`).
    Exit: 1 if any repository that holds the commit says it is not on origin/main (round 7), else 2 if there is none or
    any is unknown and may hold the commit (round 8; round 10: a repository git could not read may hold it, one that
    plainly lacks it does not), else 0 if any line is a yes, else 2 if any is unknown, else 1. Line order is not pinned. The workspace's
    own repository has three worktrees, so it is one line named `wt-dirty`, the first by name. A pruned worktree is a tree of its
    repository (#294 round 10), so it joins that repository's one line rather than adding one.
    """

    def setUp(self) -> None:
        tmp, self.root = workspace(ShaFlagTest.ROW, ShaFlagTest.LOG)
        self.addCleanup(tmp.cleanup)
        self.clone, self.trees = self.root / "repo", self.root / "trees"
        self.sha = git("rev-parse", "origin/main", cwd=self.clone)
        self.tip = git("rev-parse", "--short", "origin/main", cwd=self.clone)

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = al.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def docs_clone(self, dest: Path) -> Path:
        """A clone of a different repository (its own bare origin and history)."""
        bare = self.root / "docs.git"
        if not bare.exists():
            bare.mkdir()
            git("init", "--bare", "--initial-branch=main", ".", cwd=bare)
            seed = self.root / "docs-seed"
            seed.mkdir()
            git("init", "--initial-branch=main", ".", cwd=seed)
            git("config", "user.email", "d@example.test", cwd=seed)
            git("config", "user.name", "Docs", cwd=seed)
            (seed / "docs.md").write_text("documentation, not the project\n")
            git("add", "docs.md", cwd=seed)
            git("commit", "-m", "docs", cwd=seed)
            git("remote", "add", "origin", str(bare), cwd=seed)
            git("push", "-q", "origin", "main", cwd=seed)
        git("clone", "-q", str(bare), str(dest), cwd=self.root)
        return dest

    OVERALL = {0: "overall on origin/main", 1: "overall not on origin/main", 2: "overall unknown"}

    def lines(self, out: str) -> list[str]:
        """The per-repository lines, sorted: the last line is the overall one (`assert_overall`)."""
        return sorted(out.splitlines()[:-1])

    def assert_overall(self, out: str, sha: str, code: int) -> None:
        """#49 round 9, 3: with more than one repository, one last stdout line says the verdict the exit code carries,
        since a run piped through `head` or chained with `;` loses the code."""
        self.assertEqual(out.splitlines()[-1], f"sha {sha}: {self.OVERALL[code]}", out)

    def test_a_clone_of_another_repository_beside_the_project_is_asked_too(self) -> None:
        docs = self.docs_clone(self.trees / "aaa-docs")
        docs_tip = git("rev-parse", "--short", "origin/main", cwd=docs)
        code, out, _ = self.run_main("--sha", self.sha)
        self.assertEqual(code, 0)
        self.assertEqual(self.lines(out), sorted([
            f"sha {self.sha}: on origin/main {self.tip} (fetched) [wt-dirty]",
            f"sha {self.sha}: no such commit after fetch, so not on origin/main {docs_tip} [aaa-docs]"]))
        self.assert_overall(out, self.sha, 0)

    def test_trees_sharing_one_repository_are_one_line(self) -> None:
        code, out, _ = self.run_main("--sha", self.sha)
        self.assertEqual((code, out), (0, f"sha {self.sha}: on origin/main {self.tip} (fetched) [wt-dirty]\n"))

    def test_a_link_to_a_repository_outside_the_root_is_neither_asked_nor_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as away:
            outside = self.docs_clone(Path(away) / "outside")
            fetch_head = outside / ".git" / "FETCH_HEAD"
            self.assertFalse(fetch_head.exists())
            (self.trees / "aaa-link").symlink_to(outside)
            code, out, _ = self.run_main("--sha", self.sha)
            self.assertFalse(fetch_head.exists(), "the audit fetched into a repository outside the workspace root")
        self.assertEqual((code, out), (0, f"sha {self.sha}: on origin/main {self.tip} (fetched) [wt-dirty]\n"))
        self.assertNotIn("aaa-link", out)

    def dead_file(self, name: str) -> Path:
        """A tree whose `.git` FILE points at a repository that is not there at all (no `<common>` behind it)."""
        tree = self.trees / name
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {self.root / 'no-such-gitdir'}\n")
        return tree

    def dead_worktree(self, name: str) -> Path:
        """A pruned worktree: a real `git worktree add` whose `<common>/worktrees/<name>` was then deleted, the repository
        itself still there (the stale worktree of #294)."""
        git("worktree", "add", "-b", f"feat/{name}", str(self.trees / name), "main", cwd=self.clone)
        shutil.rmtree(self.clone / ".git" / "worktrees" / name)
        return self.trees / name

    def pruned_line(self, name: str) -> str:
        """#294 round 7: a pruned worktree is answered by its repository, so for the project's `origin/main` commit it says
        what the main clone says."""
        return f"sha {self.sha}: on origin/main {self.tip} (fetched) [{name}]"

    def unread_line(self, name: str) -> str:
        return f"sha {self.sha}: unknown \u2014 git could not read this repository [{name}]"

    def run_counting_fetches(self, sha: str) -> tuple[int, str, str, list[Path]]:
        """`run_main("--sha", sha)` plus the repository each `git_trees.fetch_base` call that could reach one went to: the
        resolved common git dir of the tree it was called on. A fetch on a pruned tree itself (git cannot read it, so it
        reaches nothing) is not counted; the redirect to `<common>` is, as is a fetch on a healthy tree."""
        real, reached = git_trees.fetch_base, []

        def counting(tree, *rest, **kw):
            try:  # from files, as `sha_trees` does: the view's own path is a temp dir, not the repository
                reached.append(git_view.locate(Path(tree)).common.resolve())
            except git_view.Unviewable:
                pass
            return real(tree, *rest, **kw)

        with patch.object(git_trees, "fetch_base", counting):
            return (*self.run_main("--sha", sha), reached)

    def test_a_pruned_worktree_does_not_stop_a_healthy_one_answering(self) -> None:
        """#294, the live shape: a pruned worktree (its `.git` file names `<common>/worktrees/<name>`, which is gone, `<common>`
        is not) of the same repository as a healthy tree that says yes. It is a tree of that repository (round 10), so it is
        not a second repository: one line, the healthy tree's, exit 0, and no overall line (one repository)."""
        self.dead_worktree("zzz-dead")
        code, out, err = self.run_main("--sha", self.sha)
        self.assertEqual((code, out, err), (0, f"sha {self.sha}: on origin/main {self.tip} (fetched) [wt-dirty]\n", ""))

    def test_a_dot_git_file_pointing_at_a_repository_that_is_gone_blocks_the_yes(self) -> None:
        """#294 round 2: `.git` names a path with no repository behind it. Nothing says a repository forgot the tree, so it
        may be the project's (an unmounted volume): its unknown blocks the project's yes. Exit 2, `overall unknown`."""
        self.dead_file("aaa-broken")
        code, out, err = self.run_main("--sha", self.sha)
        self.assertEqual(self.lines(out), sorted([
            f"sha {self.sha}: on origin/main {self.tip} (fetched) [wt-dirty]", self.unread_line("aaa-broken")]))
        self.assert_overall(out, self.sha, 2)
        self.assertEqual((code, err), (2, ""))

    def test_a_worktree_whose_main_clone_was_moved_does_not_let_another_trees_yes_through(self) -> None:
        """#294 round 2, the security case: the project is a set of linked worktrees; `feat/open`'s commit is unmerged there,
        and the main clone is then renamed away (`<common>` is gone). Beside them a fresh `git init` tree whose own
        origin/main holds that commit says yes. The project's tree may hold the commit, unmerged: exit 2, not 0."""
        unmerged = git("rev-parse", "feat/open", cwd=self.clone)
        bare = self.root / "other.git"
        bare.mkdir()
        git("init", "--bare", "--initial-branch=main", ".", cwd=bare)
        git("push", "-q", str(bare), "feat/open:main", cwd=self.clone)
        fresh = self.trees / "aaa-fresh"
        fresh.mkdir()
        git("init", "--initial-branch=main", ".", cwd=fresh)
        git("remote", "add", "origin", str(bare), cwd=fresh)
        code, out, _ = self.run_main("--sha", unmerged)
        self.assertIn(f"sha {unmerged}: on origin/main {unmerged[:7]} (fetched) [aaa-fresh]", out.splitlines())  # the yes is real
        self.clone.rename(self.root / "repo-moved")
        code, out, err = self.run_main("--sha", unmerged)
        self.assert_overall(out, unmerged, 2)
        self.assertEqual((code, err), (2, ""))
        self.assertIn(f"sha {unmerged}: on origin/main {unmerged[:7]} (fetched) [aaa-fresh]", out.splitlines())
        self.assertIn(f"sha {unmerged}: unknown \u2014 git could not read this repository [wt-dirty]", out.splitlines())

    def test_a_pruned_worktree_whose_repository_holds_the_commit_unmerged_does_not_let_another_trees_yes_through(self) -> None:
        """#294 round 7, the security case: pruning a worktree entry does not remove the commit from `<common>`. The project's
        only tree under the worktrees directory is a pruned worktree; its main clone lives OUTSIDE that directory (so it is
        not listed) and holds `feat/open`'s commit unmerged on a branch. Beside it a fresh `git init` tree whose own
        origin/main holds the commit says yes. The repository answers for the pruned tree: `not on origin/main`, a holder
        says no, exit 1, not 0."""
        unmerged = git("rev-parse", "feat/open", cwd=self.clone)
        shutil.rmtree(self.trees)  # the project's linked trees are gone: only the pruned worktree below is left
        self.trees.mkdir()
        self.dead_worktree("zzz-dead")
        bare = self.root / "other.git"
        bare.mkdir()
        git("init", "--bare", "--initial-branch=main", ".", cwd=bare)
        git("push", "-q", str(bare), "feat/open:main", cwd=self.clone)
        fresh = self.trees / "aaa-fresh"
        fresh.mkdir()
        git("init", "--initial-branch=main", ".", cwd=fresh)
        git("remote", "add", "origin", str(bare), cwd=fresh)
        self.assertEqual(git_trees.check_sha(unmerged, fresh)[0], 0)  # the yes is real
        code, out, err = self.run_main("--sha", unmerged)
        self.assertEqual((code, err), (1, ""), out)
        self.assertEqual(self.lines(out), sorted([
            f"sha {unmerged}: on origin/main {unmerged[:7]} (fetched) [aaa-fresh]",
            f"sha {unmerged}: not on origin/main {self.tip} (fetched) [zzz-dead]"]))
        self.assert_overall(out, unmerged, 1)

    def test_a_healthy_tree_and_two_pruned_worktrees_of_one_repository_are_one_line_and_one_fetch(self) -> None:
        """#294 round 10, A: the healthy tree (`wt-dirty`, first by name) and two pruned worktrees (`zzz-*`) of one repository
        are one repository: exit 0, exactly one line, no overall line, and the repository is fetched once. Counted: the
        `fetch_base` calls whose tree resolves to a repository, per resolved repository (a fetch on a dead tree itself reaches
        none). Before, each pruned tree was listed and its repository fetched again for each, so one transient failure
        among the duplicates turned the repository's yes into `overall unknown`."""
        self.dead_worktree("zzz-dead")
        self.dead_worktree("zzz-dead2")
        code, out, err, reached = self.run_counting_fetches(self.sha)
        self.assertEqual((code, out, err), (0, f"sha {self.sha}: on origin/main {self.tip} (fetched) [wt-dirty]\n", ""))
        self.assertEqual(reached, [(self.clone / ".git").resolve()])

    def test_pruned_worktrees_that_sort_before_the_healthy_tree_are_the_one_line_and_one_fetch(self) -> None:
        """#294 round 10, A: the first by name is the one listed, whichever kind it is. `aaa-dead` sorts before `wt-dirty`, so
        the line is `[aaa-dead]` (answered by its repository: a yes) and the healthy tree and `zzz-dead` are not listed."""
        self.dead_worktree("aaa-dead")
        self.dead_worktree("zzz-dead")
        code, out, err, reached = self.run_counting_fetches(self.sha)
        self.assertEqual((code, out, err), (0, self.pruned_line("aaa-dead") + "\n", ""))
        self.assertEqual(reached, [(self.clone / ".git").resolve()])

    def test_pruned_trees_alone_are_answered_by_their_repository(self) -> None:
        """#294 round 7, round 10: with the project's only trees pruned worktrees, the repository still answers for them, and
        they are one repository however many: one line (the first by name), no overall line. Here: yes, exit 0."""
        shutil.rmtree(self.trees)
        self.trees.mkdir()
        self.dead_worktree("aaa-dead")
        code, out, _ = self.run_main("--sha", self.sha)
        self.assertEqual((code, out), (0, self.pruned_line("aaa-dead") + "\n"))
        self.dead_worktree("zzz-dead")
        code, out, _ = self.run_main("--sha", self.sha)
        self.assertEqual((code, out), (0, self.pruned_line("aaa-dead") + "\n"))

    def test_a_repository_git_can_open_but_not_read_still_blocks_the_yes(self) -> None:
        """#294 keeps the other half of round 10, 2: `rev-parse --git-dir` succeeds but the commit lookup faults, so the
        repository may hold the commit and blocks the project's yes (exit 2). Only that call is made to fail, for the
        tree named `aaa-open`; a pruned worktree beside it is a tree of the healthy tree's repository (round 10), so it adds
        no line and changes nothing."""
        self.dead_worktree("zzz-dead")
        self.docs_clone(self.trees / "aaa-open")
        real = git_trees.git

        def fake(args, cwd, *rest, **kw):
            if Path(cwd).name == "aaa-open" and args[:1] == ["rev-parse"] and args[1].startswith("--disambiguate="):
                return 128, "fatal: boom"
            return real(args, cwd, *rest, **kw)

        with patch.object(git_trees, "git", fake):
            code, out, _ = self.run_main("--sha", self.sha)
        self.assertEqual(self.lines(out), sorted([
            f"sha {self.sha}: on origin/main {self.tip} (fetched) [wt-dirty]",
            f"sha {self.sha}: unknown \u2014 git could not read this repository [aaa-open]"]))
        self.assert_overall(out, self.sha, 2)
        self.assertEqual(code, 2)

    def test_a_repository_with_no_remote_beside_the_project_does_not_block_a_yes(self) -> None:
        """#49 round 10, 2: `docs` is a `git init` with one commit and no remote: its unknown (no origin/main) does not hold
        the commit, so the project's yes stands. Exit 0, all three lines, the overall one saying on origin/main."""
        docs = self.trees / "docs"
        docs.mkdir()
        git("init", "--initial-branch=main", ".", cwd=docs)
        git("config", "user.email", "d@example.test", cwd=docs)
        git("config", "user.name", "Docs", cwd=docs)
        (docs / "docs.md").write_text("documentation, not the project\n")
        git("add", "docs.md", cwd=docs)
        git("commit", "-m", "docs", cwd=docs)
        code, out, err = self.run_main("--sha", self.sha)
        self.assertEqual(self.lines(out), sorted([
            f"sha {self.sha}: on origin/main {self.tip} (fetched) [wt-dirty]",
            f"sha {self.sha}: unknown \u2014 no origin/main to check against [docs]"]))
        self.assert_overall(out, self.sha, 0)
        self.assertEqual((code, err), (0, ""))

    def test_the_exit_is_no_if_a_holder_says_no_then_unknown_then_yes_then_no(self) -> None:
        docs = self.docs_clone(self.trees / "aaa-docs")
        docs_tip = git("rev-parse", "--short", "origin/main", cwd=docs)
        unmerged = git("rev-parse", "feat/open", cwd=self.clone)
        code, out, _ = self.run_main("--sha", unmerged)
        self.assertEqual((code, self.lines(out)), (1, sorted([
            f"sha {unmerged}: not on origin/main {self.tip} (fetched) [wt-dirty]",
            f"sha {unmerged}: no such commit after fetch, so not on origin/main {docs_tip} [aaa-docs]"])))
        self.assert_overall(out, unmerged, 1)
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=docs)
        nobody = "0123456789abcdef0123456789abcdef01234567"
        # a repository that holds the commit and says no outranks an unknown elsewhere; an unknown that never had the commit
        # blocks no yes (round 10, 2), and is the answer only when nothing else is
        for what, sha, want in (("a holder says no, another is unknown", unmerged, 1),
                                ("no such commit, another is unknown", nobody, 2),
                                ("yes, another is unknown and never had it", self.sha, 0)):
            with self.subTest(what):
                code, out, _ = self.run_main("--sha", sha)
                lines = out.splitlines()
                self.assertEqual((code, len(lines)), (want, 3), out)  # two repositories and the overall line
                self.assert_overall(out, sha, want)
                unknown = [l for l in lines if l.startswith(f"sha {sha}: unknown \u2014 fetch failed: ")]
                self.assertEqual(len(unknown), 1, out)
                self.assertTrue(unknown[0].endswith(" [aaa-docs]"), unknown[0])

    def test_the_exit_over_every_mix_of_answers(self) -> None:
        """#49 round 8, 2; round 10, 2: over the per-repository `(code, line, held)`: any repository that HOLDS the commit
        and says no (code 1, held) is 1; else an unknown that holds it (or none at all) is 2; else any yes is 0; else any
        unknown is 2; else 1. Every line still prints."""
        yes, unknown, unknown_held = (0, "yes", True), (2, "unknown", False), (2, "unknown, holds it", True)
        no_held, no_absent, local_only = (1, "no, held", True), (1, "no, absent", False), (1, "local main only", True)
        table = [
            ("a holder says no, another says yes", [no_held, yes], 1),
            ("local main only, another says yes", [local_only, yes], 1),
            ("a holder says no, another is unknown", [no_held, unknown], 1),
            ("a holder says no, yes and unknown", [yes, no_held, unknown], 1),
            ("yes alone", [yes], 0),
            ("yes and a no that never had it", [yes, no_absent], 0),
            ("yes and an unknown that does not hold it", [yes, unknown], 0),
            ("yes and an unknown that holds it", [yes, unknown_held], 2),
            ("yes, an unknown that holds it and one that does not", [unknown, yes, unknown_held], 2),
            ("unknown alone", [unknown], 2),
            ("unknown and a no that never had it", [no_absent, unknown], 2),
            ("no alone, never had it", [no_absent], 1),
            ("no alone, held", [no_held], 1),
            ("two nos", [no_absent, no_held], 1),
        ]
        for what, answers, want in table:
            with self.subTest(what):
                trees = [(f"t{i}", self.trees) for i in range(len(answers))]
                seen = iter([(code, f"{line} {i}", held) for i, (code, line, held) in enumerate(answers)])
                with patch.object(git_trees, "sha_trees", return_value=trees), \
                        patch.object(git_trees, "check_sha", lambda sha, tree, **kw: next(seen)):
                    code, out, _ = self.run_main("--sha", self.sha)
                self.assertEqual(code, want, out)
                per_repo = [f"{line} {i} [t{i}]" for i, (_, line, _) in enumerate(answers)]
                # round 9, 3: more than one repository adds the overall line; one repository adds none
                self.assertEqual(out.splitlines(), per_repo + ([f"sha {self.sha}: {self.OVERALL[want]}"] if len(answers) > 1 else []))

    def test_more_than_one_repository_ends_with_the_overall_answer_one_repository_does_not(self) -> None:
        """#49 round 9, 3: the table over the three exits, each with two repositories, then with one. Exit 0 says
        `overall on origin/main`, 1 `overall not on origin/main`, 2 `overall unknown`; it is the last line, exactly one
        line after the per-repository ones, and no line is added for a single repository."""
        yes, no, unknown = (0, "yes", True), (1, "no", True), (2, "unknown", False)
        for want, answers in ((0, [yes, yes]), (1, [no, yes]), (2, [unknown, unknown])):
            for count in (2, 1):
                with self.subTest(exit=want, repositories=count):
                    chosen = answers[:count]
                    trees = [(f"t{i}", self.trees) for i in range(len(chosen))]
                    seen = iter([(code, f"{line} {i}", held) for i, (code, line, held) in enumerate(chosen)])
                    with patch.object(git_trees, "sha_trees", return_value=trees), \
                            patch.object(git_trees, "check_sha", lambda sha, tree, **kw: next(seen)):
                        code, out, _ = self.run_main("--sha", self.sha)
                    self.assertEqual(code, want, out)
                    per_repo = [f"{line} {i} [t{i}]" for i, (_, line, _) in enumerate(chosen)]
                    self.assertEqual(out.splitlines(), per_repo + ([f"sha {self.sha}: {self.OVERALL[want]}"] if count == 2 else []))

    def test_a_clone_of_the_project_that_has_the_unpushed_commit_cannot_outvote_the_project(self) -> None:
        """#49 round 7, 1a: the commit is only on the project's local main. A clone of the project (its origin is the
        project) fetches it into its origin/main and says yes; the project says `on local main only`. Exit 1."""
        (self.clone / "local.txt").write_text("local\n")
        git("add", "local.txt", cwd=self.clone)
        git("commit", "-m", "local only", cwd=self.clone)
        sha = git("rev-parse", "main", cwd=self.clone)
        git("clone", "-q", str(self.clone), str(self.trees / "proj2"), cwd=self.root)
        code, out, _ = self.run_main("--sha", sha)
        self.assertEqual(self.lines(out), sorted([
            f"sha {sha}: on local main only, not pushed to origin/main {self.tip} (fetched) [wt-dirty]",
            f"sha {sha}: on origin/main {git('rev-parse', '--short', 'origin/main', cwd=self.trees / 'proj2')} (fetched) [proj2]"]))
        self.assertEqual(code, 1)
        self.assert_overall(out, sha, 1)

    def test_a_tree_whose_origin_holds_the_unmerged_commit_cannot_outvote_the_project(self) -> None:
        """#49 round 7, 1b: `trees/evil` is a fresh `git init` whose origin is a bare repository with the project's
        unmerged commit on its main. It says yes; the project holds the commit on a branch and says no. Exit 1."""
        unmerged = git("rev-parse", "feat/open", cwd=self.clone)
        bare = self.root / "evil-origin.git"
        bare.mkdir()
        git("init", "--bare", "--initial-branch=main", ".", cwd=bare)
        git("push", "-q", str(bare), "feat/open:main", cwd=self.clone)
        evil = self.trees / "evil"
        evil.mkdir()
        git("init", "--initial-branch=main", ".", cwd=evil)
        git("remote", "add", "origin", str(bare), cwd=evil)
        code, out, _ = self.run_main("--sha", unmerged)
        self.assertEqual(self.lines(out), sorted([
            f"sha {unmerged}: not on origin/main {self.tip} (fetched) [wt-dirty]",
            f"sha {unmerged}: on origin/main {git('rev-parse', '--short', 'origin/main', cwd=evil)} (fetched) [evil]"]))
        self.assertEqual(code, 1)
        self.assert_overall(out, unmerged, 1)

    def test_an_unreachable_project_remote_blocks_a_yes_from_a_fresh_init(self) -> None:
        """#49 round 8, 2: the project's remote is unreachable (its fetch fails; it holds the commit on a branch), and
        `trees/evil`, a fresh `git init` whose origin is a bare repository with that commit on its main, says yes. The
        unknown may be the very project the claim is about: exit 2, both lines printed."""
        unmerged = git("rev-parse", "feat/open", cwd=self.clone)
        bare = self.root / "evil-origin.git"
        bare.mkdir()
        git("init", "--bare", "--initial-branch=main", ".", cwd=bare)
        git("push", "-q", str(bare), "feat/open:main", cwd=self.clone)
        evil = self.trees / "evil"
        evil.mkdir()
        git("init", "--initial-branch=main", ".", cwd=evil)
        git("remote", "add", "origin", str(bare), cwd=evil)
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        code, out, _ = self.run_main("--sha", unmerged)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3, out)  # two repositories and the overall line
        self.assert_overall(out, unmerged, 2)
        self.assertEqual(sum(l.startswith(f"sha {unmerged}: unknown \u2014 fetch failed: ") and l.endswith(" [wt-dirty]") for l in lines), 1, out)
        self.assertIn(f"sha {unmerged}: on origin/main {git('rev-parse', '--short', 'origin/main', cwd=evil)} (fetched) [evil]", lines)
        self.assertEqual(code, 2)

    def test_a_fetch_error_a_remote_controls_is_one_clean_line_per_repository(self) -> None:
        """#49 round 8, 3a: two repositories, `fetch_base` hostile for both: two lines, nothing raw, escapes shown."""
        # `fetch_base` is patched to return raw control characters, which the real one no longer does: this pins `main`'s own
        # escaping of whatever a verdict line carries, not `fetch_base`'s.
        self.docs_clone(self.trees / "aaa-docs")
        for hostile in HOSTILE_FETCH:
            with self.subTest(hostile), patch.object(git_trees, "fetch_base", return_value=hostile):
                code, out, _ = self.run_main("--sha", self.sha)
                self.assertEqual(code, 2)
                assert_one_clean_line_each(self, out, 3)  # two repositories and the overall line, all clean
                self.assert_overall(out, self.sha, 2)
        with patch.object(git_trees, "fetch_base", return_value=HOSTILE_FETCH[0]):
            out = self.run_main("--sha", self.sha)[1]
        self.assertIn("\\r", out)
        self.assertIn("\\x1b", out)

    def test_a_root_that_is_a_clone_with_trees_under_it_is_asked_once(self) -> None:
        """#49 round 7, 3: the root is asked, as `.`, only when no tree was found. Here the root is a clone and has a
        worktree under `trees/`: one line, for the worktree."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            git("clone", "-q", str(self.root / "origin.git"), str(root), cwd=self.root)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "trees").mkdir()
            git("worktree", "add", "-q", "-b", "feat/x", str(root / "trees" / "wt-a"), "main", cwd=root)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = al.main(["--root", str(root), "--sha", self.sha])
            self.assertEqual((code, out.getvalue(), err.getvalue()),
                             (0, f"sha {self.sha}: on origin/main {self.tip} (fetched) [wt-a]\n", ""))

    def test_a_tree_name_with_a_control_character_cannot_forge_a_line(self) -> None:
        """#49 round 7, 8: the name is printed escaped (as `repr` does) inside the brackets, so stdout has one line per
        repository and no line is the forged text. A path component cannot hold `/`, so the forgery says `origin\u2215main`
        (division slash); the point is the line break, which a real forgery would carry just the same."""
        forged = f"sha {self.sha}: on origin\u2215main {self.tip} (fetched)"
        for what, ctl, shown in (("newline", "\n", "\\n"), ("carriage return", "\r", "\\r"), ("escape", "\x1b", "\\x1b"),
                                 ("DEL", "\x7f", "\\x7f"), ("C1 control (CSI)", "\x9b", "\\x9b"),
                                 ("bidi override", "\u202e", "\\u202e")):
            with self.subTest(what):
                name = f"aaa{ctl}{forged}"
                try:  # round 10, 3: skip a character only if the filesystem will not make a directory of that name
                    (self.trees / name).mkdir()
                    (self.trees / name).rmdir()
                except OSError as e:
                    self.skipTest(f"cannot create a directory named with {what}: {e}")
                self.docs_clone(self.trees / name)
                try:
                    code, out, _ = self.run_main("--sha", self.sha)
                finally:
                    shutil.rmtree(self.trees / name)
                if ctl in "\r\n":  # git_view refuses a repository path holding a line break: unreadable, so held (fail closed)
                    self.assertEqual(code, 2)
                    self.assertEqual(out.count("\n"), 3, out)  # wt-dirty, the one unreadable tree, and the overall line
                    self.assert_overall(out, self.sha, 2)
                    self.assertIn("unknown", [l for l in out.splitlines() if "[aaa" in l][0])
                else:
                    self.assertEqual(code, 0)
                    self.assertEqual(out.count("\n"), 3, out)  # wt-dirty, the one named tree, and the overall line
                    self.assert_overall(out, self.sha, 0)
                self.assertNotIn("\r", out)
                self.assertNotIn("\x1b", out)
                self.assertEqual([c for c in out.replace("\n", "") if not c.isprintable()], [], repr(out))
                self.assertFalse([l for l in out.splitlines() if l.startswith(forged)], out)
                named = [l for l in out.splitlines() if l.endswith("]") and "[aaa" in l]
                self.assertEqual(len(named), 1, out)
                self.assertIn(f"aaa{shown}", named[0])

    def test_with_no_trees_the_root_is_asked_and_named_by_a_dot(self) -> None:
        """No `Worktrees:` line, or a worktrees dir with nothing in it: the root, if it is a git tree."""
        for what, claude, mkdir in (("no worktrees line", CLAUDE.replace("- Worktrees: `trees/`\n", ""), False),
                                    ("an empty worktrees dir", CLAUDE, True)):
            with self.subTest(what), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                git("clone", "-q", str(self.root / "origin.git"), str(root), cwd=self.root)
                (root / "CLAUDE.md").write_text(claude)
                if mkdir:
                    (root / "trees").mkdir()
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = al.main(["--root", str(root), "--sha", self.sha])
                self.assertEqual((code, out.getvalue(), err.getvalue()),
                                 (0, f"sha {self.sha}: on origin/main {self.tip} (fetched) [.]\n", ""))

    def test_a_shallow_tree_gives_no_definite_no(self) -> None:
        """F, end to end: the only tree is a depth-1 clone, asked for the commit before its tip."""
        for tree in self.trees.glob("wt-*"):
            shutil.rmtree(tree)
        other = Path(tempfile.mkdtemp(dir=self.root, prefix="other-"))
        git("clone", "-q", str(self.root / "origin.git"), str(other), cwd=self.root)
        git("config", "user.email", "o@example.test", cwd=other)
        git("config", "user.name", "Other", cwd=other)
        (other / "second.txt").write_text("2\n")
        git("add", "second.txt", cwd=other)
        git("commit", "-m", "second", cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        older = git("rev-parse", "HEAD~1", cwd=other)
        git("clone", "-q", "--depth", "1", f"file://{self.root / 'origin.git'}", str(self.trees / "shallow"), cwd=self.root)
        code, out, _ = self.run_main("--sha", older)
        self.assertEqual((code, out), (2, f"sha {older}: unknown \u2014 shallow clone, so history is cut off [shallow]\n"))


NAMED_HEADER = "| name | item | owner | state | since | due | size | checklist |\n|---|---|---|---|---|---|---|---|"


def named_workspace(rows: str, log: str, sessions: str = "") -> tuple[tempfile.TemporaryDirectory, Path]:
    """`workspace` with the Tasks table carrying the `name` column (#51)."""
    tmp, root = workspace(rows, log, sessions)
    tracker = root / "daily" / "2026-09-17-tracker.md"
    tracker.write_text(tracker.read_text().replace(
        "| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|", NAMED_HEADER))
    return tmp, root


class OwnershipRowKeyedOnNameTest(unittest.TestCase):
    """#51: a File ownership row keyed on a task's `name` speaks for that task, exactly as one keyed on its item does.

    The dispatch side joins on the name (`task_keys`, `resolve_task`); the audit read only the item, so a
    name-keyed row attributed no tree to its task and that task's worktree was never checked for unlanded work.
    """

    ALPHA = "| alpha | Tidy the importer | robin | done 10:00 | 09:00 |  | S | Checklist: Tidy |"
    BETA = "| beta | Trim the exporter | robin | done 10:00 | 09:00 |  | S | Checklist: Trim |"

    def row(self, context: str, tree: str = "wt-a") -> al.OwnerRow:
        return al.OwnerRow(context, [tree])

    def test_a_task_whose_name_keys_the_row_reopens_for_that_trees_unlanded_work(self) -> None:
        tmp, root = named_workspace(self.ALPHA, started("alpha", "wt-unmerged"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Tidy the importer"], report.lines)
        self.assertIn("wt-unmerged", str(report.reopen[0]))

    def test_two_tasks_of_one_owner_each_get_their_own_name_keyed_tree(self) -> None:
        tmp, root = named_workspace(
            f"{self.ALPHA}\n{self.BETA}",
            started("alpha", "wt-unmerged") + "\n" + started("beta", "wt-merged"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual([r.task for r in report.reopen], ["Tidy the importer"], report.lines)
        self.assertFalse([l for l in report.lines if "ambiguous" in l], report.lines)

    def test_rows_for_takes_the_name_row_as_exact(self) -> None:
        keyed = self.row("alpha")
        rows, note = al.rows_for("Tidy the importer", "robin", [self.row("beta", "wt-b"), keyed], "alpha")
        self.assertEqual(rows, [keyed])
        self.assertEqual(note, "")

    def test_a_name_keyed_row_wins_over_owner_scoring(self) -> None:
        """Two rows for the owner and none naming the item would be `ambiguous`; the name settles it."""
        keyed = self.row("alpha", "wt-c")
        owners = [self.row("robin (first thing)"), self.row("robin (second thing)", "wt-b"), keyed]
        self.assertEqual(al.rows_for("Tidy the importer", "robin", owners)[0], [])
        rows, note = al.rows_for("Tidy the importer", "robin", owners, "alpha")
        self.assertEqual((rows, note), ([keyed], ""))

    def test_an_empty_name_changes_nothing(self) -> None:
        owners = [self.row("robin (first thing)"), self.row("robin (second thing)", "wt-b"), self.row("alpha", "wt-c")]
        self.assertEqual(al.rows_for("Tidy the importer", "robin", owners, ""),
                         al.rows_for("Tidy the importer", "robin", owners))

    def test_a_name_no_row_carries_falls_back_to_the_owners_row(self) -> None:
        mine = self.row("robin")
        self.assertEqual(al.rows_for("Tidy the importer", "robin", [mine, self.row("beta", "wt-b")], "alpha"),
                         ([mine], ""))

    def test_an_item_keyed_row_still_wins(self) -> None:
        keyed = self.row("Tidy the importer")
        self.assertEqual(al.rows_for("Tidy the importer", "robin", [keyed, self.row("beta", "wt-b")], "alpha"),
                         ([keyed], ""))

    @staticmethod
    def named(name: str, item: str, owner: str, state: str = "done 10:00") -> str:
        return f"| {name} | {item} | {owner} | {state} | 09:00 |  | S | Checklist: x |"

    def test_a_bare_row_named_for_no_listed_session_is_still_its_tasks_row(self) -> None:
        """A started line keyed on its task's name stays live through that task's listed owner."""
        tmp, root = named_workspace(
            self.named("arch", "Review the architecture", "kim", "running 09:10"),
            started("arch", "wt-unmerged"), sessions=session_row("kim"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.orphans, [], report.lines)

    def test_only_a_context_that_is_the_name_is_keyed_on_it(self) -> None:
        item = "Tidy the importer"
        for context in ("alpha (v2)", "alpha [1234]", "**alpha**", "`alpha`"):
            with self.subTest(context):
                self.assertFalse(al.keyed_on(self.row(context), item, "alpha"))
        self.assertTrue(al.keyed_on(self.row("alpha"), item, "alpha"))

    def test_the_item_arm_still_reads_a_bare_context(self) -> None:
        """Only the name arm tightens: a row keyed on the item keeps `owns`'s reading, parenthetical and all."""
        self.assertTrue(al.keyed_on(self.row("Tidy the importer (v2)"), "Tidy the importer", "alpha"))

    def test_a_row_keyed_on_another_tasks_name_is_not_this_tasks(self) -> None:
        other = self.row("beta", "wt-b")
        self.assertEqual(al.rows_for("Tidy the importer", "robin", [other], "alpha"), ([], ""))
        owners = [self.row("robin (first thing)"), self.row("robin (second thing)", "wt-c"), other]
        rows, note = al.rows_for("Tidy the importer", "robin", owners, "alpha")
        self.assertEqual(rows, [])
        self.assertIn("ambiguous", note)

    # A started line keyed on its task's name: the task's owner, not the row's context, is what `## Sessions` lists.
    SAM_ALPHA = "| alpha | Tidy the importer | sam | done 10:00 | 09:00 |  | S | Checklist: Tidy |"
    UNPUSHED = started("alpha", "wt-unmerged")

    def task(self, name: str, owner: str = "sam", item: str = "Tidy the importer") -> Task:
        return Task(item=item, owner=owner, state="running 09:10", since="09:00", due="", checklist="", name=name)

    def claim(self, row: al.OwnerRow, tasks: list[Task], roster: set[str]):
        # The roster is the listed sessions' names; #553's escape asks the table, so say it has rows.
        rows = tuple(Session("", name, "", "", "", "", "", "", "") for name in roster)
        return al.row_claim(row, tasks, roster, set(), "robin", sessions=rows)

    def test_a_name_keyed_rows_claim_is_live_while_its_tasks_owner_is_listed(self) -> None:
        self.assertTrue(self.claim(self.row("alpha"), [self.task("alpha")], {"sam"}).live)

    def test_an_item_keyed_rows_claim_is_live_while_its_tasks_owner_is_listed(self) -> None:
        """The shape the name-keyed case must match."""
        self.assertTrue(self.claim(self.row("Tidy the importer"), [self.task("alpha")], {"sam"}).live)

    def test_a_name_keyed_tree_with_unpushed_work_is_not_an_orphan_while_its_tasks_owner_is_listed(self) -> None:
        tmp, root = named_workspace(self.SAM_ALPHA, self.UNPUSHED, sessions=session_row("sam"))
        self.addCleanup(tmp.cleanup)
        report = al.audit(root, "2026-09-17")
        self.assertEqual(report.orphans, [], report.lines)

    def test_a_name_keyed_tree_whose_tasks_owner_is_not_listed_is_still_an_orphan(self) -> None:
        tmp, root = named_workspace(self.SAM_ALPHA, self.UNPUSHED, sessions=session_row("kim"))
        self.addCleanup(tmp.cleanup)
        [orphan] = al.audit(root, "2026-09-17").orphans
        self.assertIn("wt-unmerged", str(orphan))
        self.assertIn("is not in ## Sessions", str(orphan))

    def test_a_name_keyed_rows_claim_is_not_live_when_its_tasks_owner_is_not_listed(self) -> None:
        self.assertFalse(self.claim(self.row("alpha"), [self.task("alpha")], {"kim"}).live)

    def test_a_row_keyed_on_a_name_no_task_carries_is_not_live(self) -> None:
        self.assertFalse(self.claim(self.row("gamma"), [self.task("alpha")], {"sam"}).live)

    def test_a_row_keyed_on_another_tasks_name_is_not_live_through_this_task(self) -> None:
        tasks = [self.task("alpha", "sam"), self.task("beta", "kim", "Trim the exporter")]
        self.assertFalse(self.claim(self.row("beta"), tasks, {"sam"}).live)

    def test_a_blank_task_name_never_claims_a_row(self) -> None:
        """A row with an empty context claims through no task."""
        for context in ("", "   "):
            with self.subTest(context=repr(context)):
                row = al.OwnerRow(context, ["wt-a"])
                self.assertFalse(self.claim(row, [self.task("", "sam"), self.task("  ", "sam")], {"sam"}).live)


class AuditThroughTheViewTest(unittest.TestCase):
    """#443 slice 3: an audit pass reads the trees through `git_view`, so what a worker plants in a tree's `.git` is data.

    The workspace is `build()`'s: a clone, and the trees `wt-merged`, `wt-unmerged` and `wt-dirty` off it. The worker's
    plant goes into the clone's own config, which every linked tree reads.
    """

    ROWS = ("| Unsaved work | robin | done 10:00 | 09:00 |  | Checklist: Unsaved work |\n"
            "| Open branch work | robin | done 10:00 | 09:00 |  | Checklist: Open branch work |",
            started("Unsaved work", "wt-dirty") + "\n" + started("Open branch work", "wt-unmerged"))

    def setUp(self) -> None:
        tmp, self.root = workspace(*self.ROWS)
        self.addCleanup(tmp.cleanup)
        self.canary = self.root / "canary"
        self.config = self.root / "repo" / ".git" / "config"

    def command(self) -> str:
        code = f"from pathlib import Path; import sys; Path({str(self.canary)!r}).write_text('fired'); sys.exit(1)"
        return shlex.join([sys.executable, "-c", code])

    def test_a_configured_clones_redirected_tree_stays_unreadable(self) -> None:
        claude = self.root / "CLAUDE.md"
        claude.write_text(claude.read_text().replace("## Coordinator\n", "## Coordinator\n- Settings: `settings.toml`\n"))
        (self.root / "settings.toml").write_text('[repos]\nproject = "repo"\n')
        other, decoy = self.root / "other", self.root / "decoy"
        git("clone", str(self.root / "repo"), str(other), cwd=self.root)
        git("worktree", "add", "-b", "decoy", str(decoy), cwd=other)
        tree = self.root / "trees" / "wt-unmerged"
        admin = git_view.locate(decoy).gitdir
        (admin / "gitdir").write_text(str(tree / ".git") + "\n")
        (tree / ".git").write_text(f"gitdir: {admin}\n")
        with patch.object(git_view, "run", wraps=git_view.run) as reads:
            report = al.audit(self.root, "2026-09-17", check_issues=False)
        line = next(line for line in report.lines if line.startswith("wt-unmerged ("))
        self.assertIn(tree_state.UNREADABLE_BRANCH, line)
        self.assertFalse(any(call.args[1] == tree for call in reads.call_args_list), "a refused view is never retried unpinned")

    def test_bad_cache_is_one_configuration_error_not_per_tree_unknowns(self) -> None:
        # Bypass the test module's explicit trusted base to exercise launcher configuration.
        real_base = git_view._base
        cache = self.root / "bad-cache"
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}), \
                patch.object(git_view, "_base", side_effect=lambda layout, base: real_base(layout, None)) as bases, \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = al.main(["--root", str(self.root), "--date", "2026-09-17"])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(bases.call_count, 1)
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertIn(str(cache), err.getvalue())
        self.assertIn("cache", err.getvalue())

    def plant(self, how: str) -> None:
        if how == "fsmonitor":
            git("config", "--file", str(self.config), "core.fsmonitor", self.command(), cwd=self.root)
        else:  # an include behind a condition that matches the branch of `wt-dirty`
            included = self.root / "conditional.cfg"
            git("config", "--file", str(included), "core.fsmonitor", self.command(), cwd=self.root)
            git("config", "--file", str(self.config), "includeIf.onbranch:feat/dirty.path", str(included), cwd=self.root)

    # What the pass says about the three trees of `build()`, read through the view. Without these a pass that skipped every
    # tree, or read every tree as unreadable, would agree with itself and pass (R1).
    FACTS = ("wt-dirty (feat/dirty): 1 uncommitted file(s)", "wt-unmerged (feat/open): not on main",
             "wt-merged (landed): on main, committed")

    def test_a_worker_planted_program_does_not_run_in_an_audit_pass(self) -> None:
        clean, original = al.audit(self.root, "2026-09-17"), self.config.read_text()
        self.assertTrue(clean.reopen and clean.lines, "the pass must have something to say, or it proves nothing")
        for fact in self.FACTS:
            self.assertIn(fact, clean.lines, "the pass did not read the trees")
        self.assertFalse(self.canary.exists())
        for how in ("fsmonitor", "includeif"):
            with self.subTest(how):
                self.plant(how)
                subprocess.run(["git", "-C", str(self.root / "trees" / "wt-dirty"), "status", "--porcelain"],
                               env=git_trees.audit_env(), capture_output=True, timeout=15, check=False)
                self.assertTrue(self.canary.exists(), "the plant must fire under the former read, or the test proves nothing")
                self.canary.unlink()
                audited = al.audit(self.root, "2026-09-17")
                self.assertFalse(self.canary.exists(), "the audit ran a program the worker planted")
                self.assertEqual((audited.lines, audited.reopen), (clean.lines, clean.reopen))
                for fact in self.FACTS:
                    self.assertIn(fact, audited.lines)
                self.config.write_text(original)

    def submodule_tree(self) -> Path:
        """`wt-merged` with a gitlink (a nested repository with changes of its own), on a branch that is on main."""
        tree = self.root / "trees" / "wt-merged"
        nested = tree / "gl"
        nested.mkdir()
        git("init", "-q", "-b", "main", cwd=nested)
        git("config", "user.email", "n@example.test", cwd=nested)
        git("config", "user.name", "Nested", cwd=nested)
        (nested / "file.txt").write_text("a\n")
        git("add", "file.txt", cwd=nested)
        git("commit", "-q", "-m", "nested", cwd=nested)
        oid = git("rev-parse", "HEAD", cwd=nested)
        git("update-index", "--add", "--cacheinfo", f"160000,{oid},gl", cwd=tree)
        git("commit", "-q", "-m", "gitlink", cwd=tree)
        head = git("rev-parse", "HEAD", cwd=tree)
        for ref in ("refs/heads/main", "refs/remotes/origin/main"):  # the tree's HEAD is on main and on origin/main
            git("update-ref", ref, head, cwd=self.root / "repo")
        (nested / "file.txt").write_text("changed in the nested repository\n")
        return tree

    def test_a_submodule_tree_is_never_read_as_done(self) -> None:
        """`status` is unreadable there (`unreadable: submodules are not inspected`), so uncommitted work cannot be ruled
        out: `_state` must not say `on main, committed`. Control: the same tree without the submodule says exactly that."""
        tree = self.root / "trees" / "wt-merged"
        self.assertEqual(al._state(tree), ("landed", ""))
        self.submodule_tree()
        self.assertEqual(git_trees.git(["merge-base", "--is-ancestor", "HEAD", "main"], tree)[0], 0, "on main, as arranged")
        self.assertEqual(git_trees.git(["status", "--porcelain"], tree)[1], "unreadable: submodules are not inspected")
        branch, why = al._state(tree)
        self.assertEqual(branch, "landed")
        self.assertEqual(why, "uncommitted work unknown (status unreadable)")

    def test_a_tree_git_cannot_read_at_all_is_never_read_as_done(self) -> None:
        """R2: the same reason for a `.git` the view refuses, not only a submodule: the status is unreadable there too."""
        tree = self.root / "trees" / "wt-merged"
        refuse(tree)
        branch, why = al._state(tree)
        self.assertTrue(why.startswith("uncommitted work unknown (status unreadable)"), (branch, why))

    def test_a_submodule_tree_is_at_risk_in_the_pass(self) -> None:
        self.submodule_tree()
        state = git_trees.read_state(self.root / "trees" / "wt-merged")
        self.assertEqual((state.dirty, state.off_origin, state.unpushed), (None, None, None))
        self.assertIn(tree_state.UNCOMMITTED_UNKNOWN, tree_state.at_risk(state))

    def test_a_submodule_trees_counts_are_unknown_even_when_it_has_an_upstream_that_holds_it(self) -> None:
        """R3: the pass reads `unpushed` and `off_origin` as None, not as the 0 an upstream on origin/main would give, because
        a tree whose status is unreadable proves nothing. Without the upstream, None is legitimate and the test would pass
        with the guard gone. Then R8: the finding says the one true thing."""
        tree = self.submodule_tree()
        git("branch", "--set-upstream-to=origin/main", "landed", cwd=self.root / "repo")
        for counted in (["rev-list", "--count", "@{u}..HEAD"], ["rev-list", "--count", "origin/main..HEAD"]):
            self.assertEqual(git_trees.git(counted, tree), (0, "0"), "the premise: git counts 0 here")
        report = al.audit(self.root, "2026-09-17")
        [found] = [u for u in report.unclaimed if "wt-merged" in str(u)]
        text = str(found)
        self.assertIn("wt-merged (landed)", text)  # the branch read fine: only the status did not
        self.assertNotIn("no upstream", text)
        self.assertNotIn("0 uncommitted", text)
        self.assertIn(tree_state.UNCOMMITTED_UNKNOWN, text)
        self.assertEqual(git_trees.read_state(tree), tree_state.TreeState("landed", None, None, None))


class UnreadableTreeTest(unittest.TestCase):
    """#443 slice 3 review: a tree git could not read (128) is not git's `no` (1). Exit 1 keeps the wording it had; 128 gets
    the one `UNREADABLE_PHRASE` (it contains `unknown`) and never the false text: "not on main", "never created", NOT_LANDED.
    """

    ROWS = AuditThroughTheViewTest.ROWS

    def setUp(self) -> None:
        tmp, self.root = workspace(*self.ROWS)
        self.addCleanup(tmp.cleanup)
        self.trees = self.root / "trees"
        self.clone = self.root / "repo"

    # _state
    def test_exit_1_is_still_not_on_main(self) -> None:
        self.assertEqual(al._state(self.trees / "wt-unmerged"), ("feat/open", "not on main"))

    def test_a_merge_base_git_cannot_answer_is_unknown_not_on_main(self) -> None:
        for name, expect in (("wt-unmerged", ""), ("wt-dirty", "1 uncommitted file(s)")):
            with self.subTest(name), refusing("merge-base"):
                _, why = al._state(self.trees / name)
                self.assertNotIn("not on main", why)
                self.assertNotIn("uncommitted work unknown", why, "the status was readable")
                self.assertIn("unknown", unreadable_phrase())
                self.assertIn(unreadable_phrase(), why)
                self.assertIn(expect, why)

    def test_a_tree_the_view_refuses_is_unknown_in_both_facts_and_not_on_main_in_neither(self) -> None:
        refuse(self.trees / "wt-unmerged")
        _, why = al._state(self.trees / "wt-unmerged")
        self.assertNotIn("not on main", why)
        self.assertIn(unreadable_phrase(), why)
        self.assertIn(tree_state.UNCOMMITTED_UNKNOWN, why)

    # landed
    def test_a_sha_whose_merge_base_git_cannot_answer_is_not_not_landed(self) -> None:
        unmerged, merged = self.trees / "wt-unmerged", self.trees / "wt-merged"
        sha, landed_sha = git("rev-parse", "HEAD", cwd=unmerged), git("rev-parse", "HEAD", cwd=merged)
        self.assertEqual(al.landed(sha, unmerged), (al.NOT_LANDED, ""), "exit 1 is still the real no")
        self.assertEqual(al.landed(landed_sha, merged), (al.LANDED, ""))
        with refusing("merge-base"):
            verdict, complaint = al.landed(sha, unmerged)
        self.assertNotEqual(verdict, al.NOT_LANDED)
        self.assertEqual(verdict, al.UNKNOWN_SHA)
        self.assertIn(unreadable_phrase(), complaint)

    def test_a_done_task_whose_sha_git_cannot_place_is_not_told_its_sha_is_not_on_main(self) -> None:
        sha = git("rev-parse", "--short", "HEAD", cwd=self.trees / "wt-unmerged")
        tracker = self.root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text().replace("| Open branch work | robin | done 10:00 |", f"| Open branch work | robin | done 10:00 {sha} |"))
        self.assertIn(f"its sha {sha} is not on main", [r.why for r in al.audit(self.root, "2026-09-17").reopen], "exit 1 is still the real no")
        with refusing("merge-base"):
            report = al.audit(self.root, "2026-09-17")
        [reopen] = [r for r in report.reopen if "wt-unmerged" in r.worktree and r.task == "Open branch work"]
        self.assertNotIn("is not on main", reopen.why)
        self.assertIn(unreadable_phrase(), reopen.why)

    # missing_tree and its handle
    def test_a_branch_lookup_git_cannot_answer_is_not_a_tree_never_created(self) -> None:
        handle = self.trees / "wt-merged"
        never = al.missing_tree(handle, "wt-never-made", "feat/planned")
        self.assertIn("never created", never, "exit 1 is still the real no")
        for words in (("rev-parse", "--verify"), ("merge-base",)):
            with self.subTest(words), refusing(*words):
                line = al.missing_tree(handle, "wt-unmerged", "feat/open")
                self.assertNotIn("never created", line)
                self.assertNotIn("no branch by that name", line)
                self.assertNotIn("not on main", line)
                self.assertIn(unreadable_phrase(), line)

    def test_a_handle_the_view_refuses_reads_as_unknown_not_as_a_tree_never_created(self) -> None:
        refuse(self.trees / "wt-merged")
        line = al.missing_tree(self.trees / "wt-merged", "wt-unmerged", "feat/open")
        self.assertNotIn("never created", line)
        self.assertNotIn("no branch by that name", line)
        self.assertIn(unreadable_phrase(), line)

    def gone(self, refused: tuple[str, ...]):
        """`wt-unmerged` (branch feat/open, work only there) removed, and each of `refused` unreadable."""
        git("worktree", "remove", "--force", str(self.trees / "wt-unmerged"), cwd=self.clone)
        git("branch", "-m", "feat/open", "wt-unmerged", cwd=self.clone)  # a started line names no branch (#1039)
        for name in refused:
            refuse(self.trees / name)
        return al.audit(self.root, "2026-09-17")

    def test_the_handle_is_a_tree_that_can_be_read(self) -> None:
        """`wt-dirty` is the first tree the pass visits and the view refuses it; `wt-merged` is fine. The branch is only on
        itself, and the line must say so rather than that the tree was never created."""
        line = next(ln for ln in self.gone(("wt-dirty",)).lines if ln.startswith("wt-unmerged: no worktree"))
        self.assertNotIn("never created", line)
        self.assertIn("branch wt-unmerged", line)
        self.assertIn("not on main", line)

    def test_with_every_tree_unreadable_the_missing_one_is_not_called_never_created(self) -> None:
        line = next(ln for ln in self.gone(("wt-dirty", "wt-merged")).lines if ln.startswith("wt-unmerged: no worktree"))
        self.assertNotIn("never created", line)
        self.assertNotIn("no branch by that name", line)
        self.assertTrue(unreadable_phrase() in line or "no tree left on disk to ask git in" in line, line)

    # the finding and the reopen line
    def test_a_refused_trees_branch_is_one_placeholder_in_every_line(self) -> None:
        """R8: `wt-merged (unreadable: .git is neither a directory...)` split reopen grouping by reason and put git's text in
        the tree line. The branch is a fixed placeholder, the same whatever the reason."""
        refuse(self.trees / "wt-merged")
        refuse(self.trees / "wt-unmerged", "huge")
        report = al.audit(self.root, "2026-09-17")
        self.assertEqual(al._state(self.trees / "wt-merged")[0], al._state(self.trees / "wt-unmerged")[0])
        self.assertFalse([ln for ln in report.lines if "unreadable:" in ln], report.lines)
        self.assertFalse([r for r in report.reopen if "unreadable:" in r.worktree], report.reopen)
        [found] = [u for u in report.unclaimed if "wt-merged" in str(u)]
        self.assertNotIn("no upstream", str(found))
        self.assertNotIn("0 uncommitted", str(found))
        self.assertIn(tree_state.UNCOMMITTED_UNKNOWN, str(found))

    def test_a_done_task_over_a_refused_tree_reopens_as_unknown_not_as_not_on_main(self) -> None:
        refuse(self.trees / "wt-unmerged")
        report = al.audit(self.root, "2026-09-17")
        why = {r.why for r in report.reopen if "wt-unmerged" in r.worktree}
        self.assertEqual(len(why), 1, report.reopen)
        [why] = why
        self.assertNotIn("not on main", why)
        self.assertIn(unreadable_phrase(), why)
        self.assertIn(tree_state.UNCOMMITTED_UNKNOWN, why)

    # R5
    def test_a_tracker_named_tree_that_looks_like_an_option_is_still_read_through_the_view(self) -> None:
        """R5: a tracker naming `--git-dir`, and a ref of that name, made `missing_tree` pass the token to `git()`,
        which read the worker's tree outside the view because the argument list held `--git-dir`. Every audit read goes through
        the view, whatever the tracker says."""
        git("update-ref", "refs/heads/--git-dir", git("rev-parse", "HEAD", cwd=self.clone), cwd=self.clone)
        tracker = self.root / "daily" / "2026-09-17-tracker.md"
        tracker.write_text(tracker.read_text() + started("Option work", "--git-dir") + "\n")
        real, plain = subprocess.run, []

        def watch(argv, *a, **kw):
            if argv and argv[0] == "git" and argv[1:2] == ["-C"]:  # the former raw reader's shape: `git -C <tree> ...`
                plain.append(argv)
            return real(argv, *a, **kw)

        with patch.object(git_view, "run", wraps=git_view.run) as viewed, \
                patch.object(git_view, "in_view", wraps=git_view.in_view) as reused, \
                patch.object(subprocess, "run", watch):
            report = al.audit(self.root, "2026-09-17")
        # #548: a healthy tree's read rides the audit's opened view (`in_view`); `run` builds one per call. Both are
        # the view layer — the option-shaped name must reach git through one of them, never around it.
        asked = [c.args[0] for c in viewed.call_args_list if c.args] \
            + [c.args[1] for c in reused.call_args_list if len(c.args) > 1]
        self.assertIn("--git-dir", " ".join(" ".join(a) for a in asked),
                      "the premise: the audit asked about the option-shaped name")
        self.assertEqual(plain, [], "#443 slice 4 deleted `_raw_git`: no read starts `git -C <tree>` itself")
        self.assertFalse(hasattr(git_trees, "_raw_git"))
        self.assertTrue(report.lines)

    # R6
    def test_a_home_python_cannot_find_is_a_cache_configuration_error(self) -> None:
        """An unavailable launcher home is a configuration error, never a per-tree unknown."""
        real = git_view._base

        def homeless_for_wt_dirty(layout, base):
            if layout.tree is not None and layout.tree.name == "wt-dirty":
                with patch.dict(os.environ), patch.object(Path, "home", side_effect=RuntimeError("Could not determine home directory.")):
                    os.environ.pop("XDG_CACHE_HOME", None)
                    return real(layout, None)
            return real(layout, base)

        with patch.object(git_view, "_base", homeless_for_wt_dirty):
            with self.assertRaisesRegex(git_view.CacheError, "cache"):
                al.audit(self.root, "2026-09-17")

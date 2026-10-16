"""#279: a worker's merge claim is evidence only after the forge confirms it."""

import contextlib
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backlog
import board_sources as bs
import one_shot


class MergeClaimTest(unittest.TestCase):
    def reconcile(self, states, error="", *, claim="PR #12 merged into develop", home="GitHub issues; repo o/app", throws=False):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text(
                "## Coordinator\n- User: Robin\n- Timezone: America/Chicago\n"
                "- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n- Worktrees: `trees/`\n"
                f"- Backlog: {home}\n")
            tree = root / "trees/worker"
            (tree / ".chief-of-stuff").mkdir(parents=True)
            (tree / one_shot.RESULT).write_text(json.dumps({
                "status": "done", "reason": claim, "changes": "fixed retry jitter",
                "passed": 8, "failed": 0, "skipped": 0, "xfailed": 0}))
            day = datetime.now(one_shot.dispatch_prompt.config(root).zone).date().isoformat()
            path = root / "daily" / f"{day}-tracker.md"
            path.parent.mkdir()
            path.write_text("## Tasks\n| item | owner | state | since | due | checklist |\n"
                            "|---|---|---|---|---|---|\n"
                            "| Retry jitter | worker01 | running 09:00 | 09:00 | | Retry |\n\n## Log\n")
            stack.enter_context(mock.patch.object(one_shot, "changed_files", return_value=""))
            stack.enter_context(mock.patch.object(one_shot, "committed_changes", return_value=one_shot.NO_COMMITS))
            read = stack.enter_context(mock.patch.object(bs, "change_states", return_value=(states, error),
                                                        side_effect=ValueError("bad schema") if throws else None))
            report = one_shot.reconcile(root, day, "Retry jitter", "worker01", tree, 0)
            self.assertIn("| Robin | waiting |", path.read_text())
            return report, read.call_args

    def test_an_open_closed_or_missing_change_cannot_confirm_a_merge(self):
        for state in ("open", "closed", "missing"):
            with self.subTest(state=state):
                states = {} if state == "missing" else {"#12": bs.ChangeState("#12", state, "develop", "")}
                report, call = self.reconcile(states)
                self.assertEqual(report["status"], "human_review")
                self.assertIn("merge claim", report["reason"])
                self.assertEqual(call.args[1], {12})

    def test_the_forge_confirms_github_and_gitlab_merges_but_the_task_still_waits(self):
        for ref, claim, home in (("#12", "PR #12 merged into develop", "GitHub issues; repo o/app"),
                                 ("!12", "!12 merged into release", "GitLab; host https://gl.example; project o/app")):
            with self.subTest(ref=ref):
                report, call = self.reconcile({ref: bs.ChangeState(ref, "merged", "develop", "a" * 40)},
                                              claim=claim, home=home)
                self.assertEqual(report["status"], "done")
                self.assertIn("Forge merge verified", report["changes"])
                self.assertIn("a" * 7, report["changes"])
                self.assertEqual(call.args[1], {12})

    def test_partial_errors_and_bad_schema_never_confirm_a_merge(self):
        for error, throws in (("forbidden", False), ("", True)):
            with self.subTest(error=error, throws=throws):
                report, _ = self.reconcile({"#12": bs.ChangeState("#12", "merged", "develop", "a" * 40)},
                                           error, throws=throws)
                self.assertEqual(report["status"], "human_review")

    def test_a_merge_claim_without_a_change_reference_is_unverified(self):
        report, call = self.reconcile({}, claim="merged into develop; suite green")
        self.assertEqual(report["status"], "human_review")
        self.assertIsNone(call)

    def test_ordinary_branch_completion_does_not_read_the_forge(self):
        report, call = self.reconcile({}, claim="committed fix; PR #12 ready for review")
        self.assertEqual(report["status"], "done")
        self.assertIsNone(call)


if __name__ == "__main__":
    unittest.main()

"""Stage-label walks preserve unrelated issue labels and never write on preview."""

from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backlog  # noqa: E402
import audit_tasks  # noqa: E402
import forge  # noqa: E402
import kanban  # noqa: E402
import kanban_cli  # noqa: E402
from evals.project_cli import validate as validate_project_cli  # noqa: E402
from settings import GitHubProject, Kanban  # noqa: E402


FLOW = Kanban(
    stages=("00 - PLAN", "01 - PLAN REVIEW", "02 - TEST", "03 - BUILD", "04 - REVIEW", "05 - FIX"),
    human_review_label="!! - HUMAN REVIEW REQUIRED",
    stage_map={"plan": 0, "plan review": 1, "test": 2, "implement": 3,
               "review": 4, "triage": 4, "fix": 5, "verify": 4, "merge": 4},
    hold_stages=("plan review", "triage", "merge"),
    terminal_stages=("main",),
)


class LabelDeltaTest(unittest.TestCase):
    def test_plan_review_adds_hold_and_replaces_numbered_stage(self):
        delta = kanban.label_delta(FLOW, ("00 - PLAN", "kind::code"), "plan review")
        self.assertEqual(delta.add, ("01 - PLAN REVIEW", "!! - HUMAN REVIEW REQUIRED"))
        self.assertEqual(delta.remove, ("00 - PLAN",))

    def test_review_fix_review_loop_clears_and_readds_hold(self):
        review = kanban.label_delta(FLOW, ("04 - REVIEW", "!! - HUMAN REVIEW REQUIRED"), "fix")
        self.assertEqual(review.add, ("05 - FIX",))
        self.assertEqual(review.remove, ("04 - REVIEW", "!! - HUMAN REVIEW REQUIRED"))
        verify = kanban.label_delta(FLOW, ("05 - FIX", "severity::high"), "verify")
        self.assertEqual(verify.add, ("04 - REVIEW",))
        self.assertEqual(verify.remove, ("05 - FIX",))

    def test_repeated_move_is_noop_and_unrelated_labels_survive(self):
        labels = ("04 - REVIEW", "kind::code", "severity::high")
        delta = kanban.label_delta(FLOW, labels, "review")
        self.assertEqual((delta.add, delta.remove), ((), ()))
        self.assertIn("severity::high", kanban.after(labels, delta))

    def test_duplicate_stage_labels_are_cleaned_in_one_move(self):
        delta = kanban.label_delta(FLOW, ("00 - PLAN", "03 - BUILD", "kind::code"), "implement")
        self.assertEqual(delta.remove, ("00 - PLAN",))
        self.assertEqual(delta.add, ())

    def test_drift_names_missing_or_duplicate_stage_without_changing_it(self):
        self.assertIn("expected 03 - BUILD", kanban.drift(FLOW, ("kind::code",), "implement"))
        self.assertIn("00 - PLAN", kanban.drift(FLOW, ("00 - PLAN", "03 - BUILD"), "implement"))
        self.assertEqual(kanban.drift(FLOW, ("03 - BUILD", "kind::code"), "implement"), "")

    def test_waiting_one_shot_may_hold_without_moving_its_stage(self):
        labels = ("03 - BUILD", "!! - HUMAN REVIEW REQUIRED", "kind::code")
        self.assertEqual(kanban.drift(FLOW, labels, "implement", allow_extra_hold=True), "")
        self.assertIn("!! - HUMAN REVIEW REQUIRED", kanban.drift(FLOW, labels, "implement"))


class ExtraHoldTest(unittest.TestCase):
    def test_preserving_the_hold_unless_released_belongs_on_keep_hold(self):
        """#799: extra_hold only detects a hold the recorded stage did not set."""
        labels = ("03 - BUILD", FLOW.human_review_label)
        self.assertTrue(kanban.extra_hold(FLOW, labels, "implement"))
        self.assertTrue(kanban.keep_hold(FLOW, labels, "implement", False))
        self.assertFalse(kanban.keep_hold(FLOW, labels, "implement", True))
        self.assertNotIn("released", kanban.extra_hold.__doc__ or "")
        self.assertIn("unless explicitly released", kanban.keep_hold.__doc__ or "")

    def test_a_stage_that_requires_the_hold_is_not_an_extra_hold(self):
        held = ("01 - PLAN REVIEW", FLOW.human_review_label)
        self.assertFalse(kanban.extra_hold(FLOW, held, "plan review"))
        self.assertFalse(kanban.keep_hold(FLOW, held, "plan review", False))

    def test_no_hold_label_is_no_extra_hold(self):
        for stage in ("implement", "plan review", None):
            self.assertFalse(kanban.extra_hold(FLOW, ("03 - BUILD",), stage))
            self.assertFalse(kanban.keep_hold(FLOW, ("03 - BUILD",), stage, False))


class HumanHoldTest(unittest.TestCase):
    def test_github_adds_only_hold_and_verifies(self):
        ref = backlog.IssueRef("team/repo", 9)
        calls = []

        def gh(args):
            calls.append(args)
            if args[:2] == ["issue", "view"]:
                held = any(cmd[:2] == ["issue", "edit"] for cmd in calls)
                labels = ["03 - BUILD", "kind::code"] + ([FLOW.human_review_label] if held else [])
                return 0, json.dumps({"labels": [{"name": x} for x in labels]}), ""
            if args[:2] == ["label", "list"]:
                return 0, json.dumps([{"name": FLOW.human_review_label}]), ""
            return 0, "", ""

        self.assertEqual(forge.add_human_hold(ref, backlog.GitHubBacklog("team/repo"), FLOW, gh=gh), "")
        edits = [cmd for cmd in calls if cmd[:2] == ["issue", "edit"]]
        self.assertEqual(edits, [["issue", "edit", "9", "-R", "team/repo", "--add-label",
                                  FLOW.human_review_label]])

    def test_gitlab_adds_only_hold_and_verifies(self):
        home = backlog.Backlog("https://labs.example.test", "team/app")
        ref = backlog.IssueRef("team/app", 7, "labs.example.test")
        before = {"labels": ["03 - BUILD", "kind::code"]}
        after = {"labels": [*before["labels"], FLOW.human_review_label]}
        with mock.patch.object(forge.backlog, "_get", side_effect=[(before, {}, ""), (after, {}, "")]), \
             mock.patch.object(forge.backlog, "existing_labels", return_value=({FLOW.human_review_label}, "")), \
             mock.patch.object(forge.backlog, "_call", return_value=(after, {}, "")) as write:
            self.assertEqual(forge.add_human_hold(ref, home, FLOW, token="test"), "")
        self.assertEqual(write.call_args.args[-1], {"add_labels": FLOW.human_review_label})

    def test_gitlab_holds_an_issue_in_another_project_on_the_same_host(self):
        home = backlog.Backlog("https://labs.example.test", "team/app", "TEAM_TOKEN")
        ref = backlog.IssueRef("team/other", 7, "labs.example.test")
        before = {"labels": ["03 - BUILD"]}
        after = {"labels": [*before["labels"], FLOW.human_review_label]}
        with mock.patch.object(forge.backlog, "_get", side_effect=[(before, {}, ""), (after, {}, "")]) as read, \
             mock.patch.object(forge.backlog, "existing_labels", return_value=({FLOW.human_review_label}, "")) as labels, \
             mock.patch.object(forge.backlog, "_call", return_value=(after, {}, "")) as write:
            self.assertEqual(forge.add_human_hold(ref, home, FLOW, token="test"), "")
        issue = "https://labs.example.test/api/v4/projects/team%2Fother/issues/7"
        self.assertEqual(read.call_args_list[0].args[0], issue)
        self.assertEqual(write.call_args.args[1], issue)
        self.assertEqual(labels.call_args.args[0], backlog.Backlog("https://labs.example.test", "team/other", "TEAM_TOKEN"))

    def test_add_human_hold_creates_a_missing_label(self):
        # #566: a hold label the repo has never seen is created, not refused — the hold is the point.
        ref = backlog.IssueRef("team/repo", 9)
        calls = []
        labels = ["03 - BUILD"]

        def gh(args):
            calls.append(args)
            if args[:2] == ["issue", "view"]:
                held = any(cmd[:2] == ["issue", "edit"] for cmd in calls)
                shown = [*labels, *([FLOW.human_review_label] if held else [])]
                return 0, json.dumps({"labels": [{"name": x} for x in shown]}), ""
            if args[:2] == ["label", "list"]:
                return 0, json.dumps([{"name": x} for x in labels]), ""
            if args[:2] == ["label", "create"]:
                labels.append(args[2])
            return 0, "", ""

        self.assertEqual(forge.add_human_hold(ref, backlog.GitHubBacklog("team/repo"), FLOW, gh=gh), "")
        self.assertIn(["label", "create", FLOW.human_review_label, "-R", "team/repo", "--color", "428bca"], calls)
        self.assertEqual(calls[-1][:2], ["issue", "view"])  # the hold is verified after it lands

    def test_a_gitlab_hold_creates_a_missing_label(self):
        # #566: the GitLab path creates the label through ensure_labels, then applies and verifies the hold.
        home = backlog.Backlog("https://labs.example.test", "team/app")
        ref = backlog.IssueRef("team/app", 7, "labs.example.test")
        before = {"labels": ["03 - BUILD", "kind::code"]}
        held = {"labels": [*before["labels"], FLOW.human_review_label]}
        writes = []
        with mock.patch.object(forge.backlog, "_get", side_effect=[(before, {}, ""), (held, {}, "")]), \
             mock.patch.object(forge.backlog, "existing_labels", return_value=({"03 - BUILD"}, "")), \
             mock.patch.object(forge.backlog, "_call",
                               side_effect=lambda *a: (writes.append(a) or (held, {}, ""))):
            self.assertEqual(forge.add_human_hold(ref, home, FLOW, token="test"), "")
        self.assertEqual([c[4] for c in writes], [{"name": FLOW.human_review_label, "color": "#428bca"},
                                                  {"add_labels": FLOW.human_review_label}])


class GitLabUpdaterTest(unittest.TestCase):
    def setUp(self):
        self.cfg = backlog.Backlog("https://labs.example.test", "team/app")
        self.ref = backlog.IssueRef("team/app", 7, "labs.example.test")
        self.before = {"iid": 7, "state": "opened", "labels": ["00 - PLAN", "kind::code"]}
        self.after = {"iid": 7, "state": "opened", "labels": ["01 - PLAN REVIEW", "!! - HUMAN REVIEW REQUIRED", "kind::code"]}

    def test_dry_run_reads_but_does_not_write(self):
        with mock.patch.object(forge.backlog, "_get", return_value=(self.before, {}, "")), \
             mock.patch.object(forge.backlog, "_call") as write:
            got = forge._sync_gitlab(self.cfg, self.ref, FLOW, "plan review", token="test", commit=False)
        self.assertFalse(got.done)
        self.assertTrue(got.ok)
        self.assertEqual(got.delta.add, ("01 - PLAN REVIEW", "!! - HUMAN REVIEW REQUIRED"))
        write.assert_not_called()

    def test_commit_updates_only_managed_labels_and_verifies(self):
        with mock.patch.object(forge.backlog, "_get", side_effect=[(self.before, {}, ""), (self.after, {}, "")]), \
             mock.patch.object(forge.backlog, "existing_labels", return_value=(set(FLOW.stages) | {FLOW.human_review_label}, "")), \
             mock.patch.object(forge.backlog, "_call", return_value=(self.after, {}, "")) as write:
            got = forge._sync_gitlab(self.cfg, self.ref, FLOW, "plan review", token="test", commit=True,
                                     expected_stage="plan")
        self.assertTrue(got.done, got.error)
        method, _, _, _, payload = write.call_args.args
        self.assertEqual(method, "PUT")
        self.assertEqual(payload, {"add_labels": "01 - PLAN REVIEW,!! - HUMAN REVIEW REQUIRED",
                                   "remove_labels": "00 - PLAN"})

    def test_an_issue_in_another_project_on_the_host_is_read_and_written_there(self):
        cfg = backlog.Backlog("https://labs.example.test", "team/app", "TEAM_TOKEN")
        ref = backlog.IssueRef("team/other", 7, "labs.example.test")
        with mock.patch.object(forge.backlog, "_get", side_effect=[(self.before, {}, ""), (self.after, {}, "")]) as read, \
             mock.patch.object(forge.backlog, "existing_labels", return_value=(set(FLOW.stages) | {FLOW.human_review_label}, "")) as labels, \
             mock.patch.object(forge.backlog, "_call", return_value=(self.after, {}, "")) as write:
            got = forge._sync_gitlab(cfg, ref, FLOW, "plan review", token="test", commit=True, expected_stage="plan")
        self.assertTrue(got.done, got.error)
        issue = "https://labs.example.test/api/v4/projects/team%2Fother/issues/7"
        self.assertEqual([c.args[0] for c in read.call_args_list], [issue, issue])
        self.assertEqual(write.call_args.args[1], issue)
        self.assertEqual(labels.call_args.args[0], backlog.Backlog("https://labs.example.test", "team/other", "TEAM_TOKEN"))

    def test_refuses_missing_configured_label_without_writing(self):
        with mock.patch.object(forge.backlog, "_get", return_value=(self.before, {}, "")), \
             mock.patch.object(forge.backlog, "existing_labels", return_value=({"00 - PLAN"}, "")), \
             mock.patch.object(forge.backlog, "_call") as write:
            got = forge._sync_gitlab(self.cfg, self.ref, FLOW, "plan review", token="test", commit=True,
                                     expected_stage="plan")
        self.assertIn("missing label", got.error)
        write.assert_not_called()

    def test_missing_terminal_label_names_the_label_to_create(self):
        before = dict(self.before, labels=["04 - REVIEW", "kind::code"])
        with mock.patch.object(forge.backlog, "_get", return_value=(before, {}, "")), \
             mock.patch.object(forge.backlog, "existing_labels", return_value=(set(FLOW.stages), "")), \
             mock.patch.object(forge.backlog, "_call") as write:
            got = forge._sync_gitlab(self.cfg, self.ref, FLOW, "main", token="test", commit=True,
                                     expected_stage="review")
        self.assertIn("create", got.error)
        self.assertIn("main", got.error)
        write.assert_not_called()

    def test_terminal_label_transition_preserves_unrelated_labels(self):
        before = dict(self.before, labels=["04 - REVIEW", "kind::code"])
        after = dict(before, labels=["main", "kind::code"])
        with mock.patch.object(forge.backlog, "_get", side_effect=[(before, {}, ""), (after, {}, "")]), \
             mock.patch.object(forge.backlog, "existing_labels", return_value=(set(FLOW.stages) | {"main"}, "")), \
             mock.patch.object(forge.backlog, "_call", return_value=(after, {}, "")) as write:
            got = forge._sync_gitlab(self.cfg, self.ref, FLOW, "main", token="test", commit=True,
                                     expected_stage="review")
        self.assertTrue(got.done, got.error)
        self.assertEqual(write.call_args.args[4], {"add_labels": "main", "remove_labels": "04 - REVIEW"})

    def test_manual_move_is_reported_before_write(self):
        moved = dict(self.before, labels=["05 - FIX", "kind::code"])
        with mock.patch.object(forge.backlog, "_get", return_value=(moved, {}, "")), \
             mock.patch.object(forge.backlog, "_call") as write:
            got = forge._sync_gitlab(self.cfg, self.ref, FLOW, "plan review", token="test", commit=True,
                                     expected_stage="plan")
        self.assertIn("changed outside the tracker", got.error)
        write.assert_not_called()

    def test_a_launcher_hold_moves_with_its_stage(self):
        # A one-shot's review hold sits beside a non-hold stage; no --from-stage matched it, so five cards stuck.
        held = dict(self.before, labels=["02 - TEST", "!! - HUMAN REVIEW REQUIRED", "kind::code"])
        moved = dict(held, labels=["04 - REVIEW", "!! - HUMAN REVIEW REQUIRED", "kind::code"])
        with mock.patch.object(forge.backlog, "_get", side_effect=[(held, {}, ""), (moved, {}, "")]), \
             mock.patch.object(forge.backlog, "existing_labels", return_value=(set(FLOW.stages) | {FLOW.human_review_label}, "")), \
             mock.patch.object(forge.backlog, "_call", return_value=(moved, {}, "")) as write:
            got = forge._sync_gitlab(self.cfg, self.ref, FLOW, "review", token="test", commit=True,
                                     expected_stage="test")
        self.assertEqual(got.error, "")
        # Only the user clears a hold the stage did not set.
        self.assertEqual(write.call_args.args[4], {"add_labels": "04 - REVIEW", "remove_labels": "02 - TEST"})

    def test_a_card_holding_only_the_hold_can_be_initialized(self):
        held = dict(self.before, labels=["!! - HUMAN REVIEW REQUIRED", "kind::code"])
        moved = dict(held, labels=["04 - REVIEW", "!! - HUMAN REVIEW REQUIRED", "kind::code"])
        with mock.patch.object(forge.backlog, "_get", side_effect=[(held, {}, ""), (moved, {}, "")]), \
             mock.patch.object(forge.backlog, "existing_labels", return_value=(set(FLOW.stages) | {FLOW.human_review_label}, "")), \
             mock.patch.object(forge.backlog, "_call", return_value=(moved, {}, "")) as write:
            got = forge._sync_gitlab(self.cfg, self.ref, FLOW, "review", token="test", commit=True, initialize=True)
        self.assertEqual(got.error, "")
        self.assertEqual(write.call_args.args[4], {"add_labels": "04 - REVIEW"})

    def test_invalid_prior_stage_is_refused_without_writing(self):
        with mock.patch.object(forge.backlog, "_get") as read, \
             mock.patch.object(forge.backlog, "_call") as write:
            got = forge._sync_gitlab(self.cfg, self.ref, FLOW, "plan review", token="test", commit=True,
                                     expected_stage="invented")
        self.assertIn("unmapped prior stage", got.error)
        read.assert_not_called()
        write.assert_not_called()


class ProjectDriftTest(unittest.TestCase):
    def setUp(self):
        self.config = Kanban(FLOW.stages, FLOW.human_review_label, FLOW.stage_map,
                             FLOW.hold_stages, FLOW.terminal_stages,
                             GitHubProject("example", 3, "Status", ("Plan", "Plan Review", "Test", "Build", "Review", "Fix")))

    def test_project_drift_checks_status_and_independent_hold(self):
        self.assertIn("Plan Review", kanban.drift(self.config, (), "plan review", project_status="Plan"))
        self.assertIn("!! - HUMAN REVIEW REQUIRED", kanban.drift(
            self.config, (), "plan review", project_status="Plan Review"))
        self.assertEqual(kanban.drift(self.config, (FLOW.human_review_label,),
                                      "plan review", project_status="Plan Review"), "")




class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Zach\n- Timezone: America/Chicago\n"
            "- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            "- Settings: `chief-of-stuff.toml`\n"
            "- Backlog: GitLab issues; host https://labs.example.test; project team/app\n")
        (self.root / "chief-of-stuff.toml").write_text(
            '[kanban]\nstages = ["00 - PLAN", "01 - PLAN REVIEW"]\n'
            'human_review_label = "!! - HUMAN REVIEW REQUIRED"\n'
            'hold_stages = ["plan review"]\n[kanban.map]\nplan = 0\n"plan review" = 1\n')
        folder = self.root / "daily"
        folder.mkdir()
        (folder / "2026-09-25-tracker.md").write_text(
            "## Tasks\n| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| Build it | Build it | worker | running 09:00 | 09:00 | | M | build | plan review | #7 | |\n")

    def test_preview_one_issue_without_write(self):
        result = kanban.Sync(backlog.IssueRef("team/app", 7, "labs.example.test"), "plan review",
                             kanban.Delta(("01 - PLAN REVIEW",), ("00 - PLAN",)))
        output = io.StringIO()
        with mock.patch.object(forge.Forge, "write_stage", return_value=result) as sync, redirect_stdout(output):
            self.assertEqual(kanban_cli.main(["--root", str(self.root), "--date", "2026-09-25", "--issue", "#7"]), 0)
        self.assertFalse(sync.call_args.kwargs["commit"])
        self.assertIn("would update", output.getvalue())

    def _two_rows_on_issue_7(self):
        """Two lane rows sharing issue #7 (#69): the feature row and an MR-fix row at a further stage."""
        folder = self.root / "daily"
        (folder / "2026-09-25-tracker.md").write_text(
            "## Tasks\n| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| Feature | 12 | worker | running 09:00 | 09:00 | | M | build | plan | #7 | |\n"
            "| Fix pipeline | 13 | worker | waiting 09:10 | 09:10 | | M | build | plan review | #7 | |\n")

    def test_two_rows_on_one_issue_are_refused_with_their_names(self):
        """#69: the refusal names the rows sharing the issue and suggests --task."""
        self._two_rows_on_issue_7()
        err = io.StringIO()
        with redirect_stderr(err):
            code = kanban_cli.main(["--root", str(self.root), "--date", "2026-09-25", "--issue", "#7"])
        self.assertEqual(code, 2)
        self.assertIn("Feature", err.getvalue())
        self.assertIn("Fix pipeline", err.getvalue())
        self.assertIn("--task", err.getvalue())

    def test_a_task_name_picks_one_row_on_a_shared_issue(self):
        result = kanban.Sync(backlog.IssueRef("team/app", 7, "labs.example.test"), "plan review",
                             kanban.Delta(("01 - PLAN REVIEW",), ("00 - PLAN",)))
        self._two_rows_on_issue_7()
        with mock.patch.object(forge.Forge, "write_stage", return_value=result) as sync, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(kanban_cli.main(
                ["--root", str(self.root), "--date", "2026-09-25", "--issue", "#7", "--task", "Fix pipeline"]), 0)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(sync.call_args.args[1], "plan review")

    def test_a_task_item_number_picks_one_row_on_a_shared_issue(self):
        """#69: --task names a row by its tracker item number or its name."""
        result = kanban.Sync(backlog.IssueRef("team/app", 7, "labs.example.test"), "plan",
                             kanban.Delta(("00 - PLAN",), ("01 - PLAN REVIEW",)))
        self._two_rows_on_issue_7()
        with mock.patch.object(forge.Forge, "write_stage", return_value=result) as sync, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(kanban_cli.main(
                ["--root", str(self.root), "--date", "2026-09-25", "--issue", "#7", "--task", "12"]), 0)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(sync.call_args.args[1], "plan")

    def test_a_task_that_names_no_row_on_the_issue_exits_2(self):
        self._two_rows_on_issue_7()
        err = io.StringIO()
        with redirect_stderr(err):
            code = kanban_cli.main(
                ["--root", str(self.root), "--date", "2026-09-25", "--issue", "#7", "--task", "Nobody"])
        self.assertEqual(code, 2)
        self.assertIn("Nobody", err.getvalue())

    def test_issue_takes_a_bare_number_or_a_hash_number(self):
        """#44: `--issue 7` and `--issue '#7'` select the same task."""
        result = kanban.Sync(backlog.IssueRef("team/app", 7, "labs.example.test"), "plan review",
                             kanban.Delta(("01 - PLAN REVIEW",), ("00 - PLAN",)))
        seen = []
        for arg in ("7", "#7"):
            with mock.patch.object(forge.Forge, "write_stage", return_value=result) as sync, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(kanban_cli.main(["--root", str(self.root), "--date", "2026-09-25", "--issue", arg]), 0)
            sync.assert_called_once()
            seen.append(sync.call_args)
        self.assertEqual(seen[0], seen[1])

    def test_commit_requires_one_issue_and_prior_stage(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertNotEqual(kanban_cli.main(["--root", str(self.root), "--date", "2026-09-25", "--commit"]), 0)
        with redirect_stdout(output):
            self.assertNotEqual(kanban_cli.main(["--root", str(self.root), "--date", "2026-09-25",
                                             "--issue", "#7", "--commit"]), 0)

    def test_completed_task_can_be_selected_to_clear_final_stage(self):
        tracker = self.root / "daily" / "2026-09-25-tracker.md"
        tracker.write_text(tracker.read_text().replace("running 09:00", "done"))
        result = kanban.Sync(backlog.IssueRef("team/app", 7, "labs.example.test"), "plan review",
                             kanban.Delta(remove=("01 - PLAN REVIEW",)))
        output = io.StringIO()
        with mock.patch.object(forge.Forge, "write_stage", return_value=result) as sync, redirect_stdout(output):
            self.assertEqual(kanban_cli.main(["--root", str(self.root), "--date", "2026-09-25"]), 0)
            self.assertEqual(output.getvalue().strip(), "[]")
            self.assertEqual(kanban_cli.main(["--root", str(self.root), "--date", "2026-09-25",
                                          "--issue", "#7"]), 0)
        sync.assert_called_once()

    def test_a_sibling_gitlab_project_keeps_that_repo_on_the_forge(self):
        # #681 regression: write_stage rebuilds IssueRef from Forge.home; home must be the sibling.
        tracker = self.root / "daily" / "2026-09-25-tracker.md"
        tracker.write_text(
            "## Tasks\n| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| Other | Other | worker | running 09:00 | 09:00 | | M | build | plan review | "
            "https://labs.example.test/team/other/-/issues/7 | |\n")
        result = kanban.Sync(backlog.IssueRef("team/other", 7, "labs.example.test"), "plan review",
                             kanban.Delta(("01 - PLAN REVIEW",), ("00 - PLAN",)))
        homes = []
        real_init = forge.Forge.__init__

        def remember(self, home, **kwargs):
            homes.append(home)
            return real_init(self, home, **kwargs)

        with mock.patch.object(forge.Forge, "__init__", remember), \
             mock.patch.object(forge.Forge, "write_stage", return_value=result), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(kanban_cli.main(
                ["--root", str(self.root), "--date", "2026-09-25", "--issue",
                 "https://labs.example.test/team/other/-/issues/7"]), 0)
        self.assertEqual([h.project for h in homes], ["team/other"])


class AuditTest(unittest.TestCase):
    def test_audit_reports_label_drift_without_writing(self):
        task = SimpleNamespace(standing=False, lane="build", stage="implement", issue="#7",
                               label="Build it", kind="open", item="Build it")
        cfg = backlog.Backlog("https://labs.example.test", "team/app")
        issue = backlog.Issue(7, "Build it", "opened", ("00 - PLAN", "kind::code"))
        with mock.patch.object(audit_tasks, "issue_states", return_value={7: issue}), \
             mock.patch.object(forge.backlog, "_call") as write:
            faults = audit_tasks.kanban_faults([task], cfg, FLOW)
        self.assertEqual(len(faults), 1)
        self.assertIn("expected 03 - BUILD", str(faults[0]))
        write.assert_not_called()

    def test_gitlab_audit_uses_labels_even_with_github_project_configured(self):
        task = SimpleNamespace(standing=False, lane="build", stage="implement", issue="#7",
                               label="Build it", kind="open", item="Build it")
        cfg = backlog.Backlog("https://labs.example.test", "team/app")
        issue = backlog.Issue(7, "Build it", "opened", ("03 - BUILD",))
        mixed = Kanban(FLOW.stages, FLOW.human_review_label, FLOW.stage_map,
                       FLOW.hold_stages, FLOW.terminal_stages, GitHubProject("example", 3, options=FLOW.stages))
        with mock.patch.object(audit_tasks, "issue_states", return_value={7: issue}):
            self.assertEqual(audit_tasks.kanban_faults([task], cfg, mixed), [])

    def test_a_failed_hold_is_reported_while_the_issue_has_no_hold(self):
        # #566: the Log recorded the launcher's failed hold; the audit keeps reporting it while the issue has no hold.
        task = SimpleNamespace(standing=False, lane="build", stage="implement", issue="#7",
                               label="Build it", kind="waiting", item="Security audit")
        cfg = backlog.Backlog("https://labs.example.test", "team/app")
        issue = backlog.Issue(7, "Build it", "opened", ("03 - BUILD",))
        with mock.patch.object(audit_tasks, "issue_states", return_value={7: issue}):
            clean = audit_tasks.kanban_faults([task], cfg, FLOW)
            failed = audit_tasks.kanban_faults([task], cfg, FLOW,
                                               hold_failures={"Security audit": "lab was down"})
        self.assertEqual(clean, [])
        self.assertEqual([f.why for f in failed], ["review hold failed: lab was down; the issue has no hold label"])

    def test_a_failed_hold_whose_hold_landed_is_not_reported(self):
        task = SimpleNamespace(standing=False, lane="build", stage="implement", issue="#7",
                               label="Build it", kind="waiting", item="Security audit")
        cfg = backlog.Backlog("https://labs.example.test", "team/app")
        issue = backlog.Issue(7, "Build it", "opened", ("03 - BUILD", FLOW.human_review_label))
        with mock.patch.object(audit_tasks, "issue_states", return_value={7: issue}):
            self.assertEqual(audit_tasks.kanban_faults([task], cfg, FLOW,
                                                       hold_failures={"Security audit": "lab was down"}), [])

    def test_github_project_audit_lists_items_once_for_multiple_tasks(self):
        cfg = backlog.GitHubBacklog("example/app")
        mixed = Kanban(FLOW.stages, FLOW.human_review_label, FLOW.stage_map,
                       FLOW.hold_stages, FLOW.terminal_stages, GitHubProject("example", 3, options=FLOW.stages))
        tasks = [SimpleNamespace(standing=False, lane="build", stage="implement",
                                 issue=f"#{n}", label=f"Build {n}", kind="open", item=f"Build {n}") for n in (7, 8)]
        issues = {n: backlog.Issue(n, f"Build {n}", "opened", ()) for n in (7, 8)}
        project_items = {backlog.IssueRef("example/app", n).url: FLOW.stages[3] for n in (7, 8)}
        with mock.patch.object(audit_tasks, "issue_states", return_value=issues), \
             mock.patch.object(forge, "project_items", return_value=(project_items, "")) as items:
            self.assertEqual(audit_tasks.kanban_faults(tasks, cfg, mixed), [])
        items.assert_called_once()

    def test_github_project_audit_matches_mixed_case_repo(self):
        cfg = backlog.GitHubBacklog("Example/App")
        mixed = Kanban(FLOW.stages, FLOW.human_review_label, FLOW.stage_map,
                       FLOW.hold_stages, FLOW.terminal_stages, GitHubProject("example", 3, options=FLOW.stages))
        task = SimpleNamespace(standing=False, lane="build", stage="implement", issue="#7",
                               label="Build it", kind="open", item="Build it")
        issue = backlog.Issue(7, "Build it", "opened", ())
        project_items = {"https://github.com/example/app/issues/7": FLOW.stages[3]}
        with mock.patch.object(audit_tasks, "issue_states", return_value={7: issue}), \
             mock.patch.object(forge, "project_items", return_value=(project_items, "")):
            self.assertEqual(audit_tasks.kanban_faults([task], cfg, mixed), [])

    def test_rows_sharing_an_issue_expect_the_furthest_stage_only(self):
        """#87: one expected stage per issue — a further-stage row makes the earlier row's stage clean."""
        feature = SimpleNamespace(standing=False, lane="build", stage="plan", issue="#7",
                                  label="Feature", kind="open", item="12")
        fix = SimpleNamespace(standing=False, lane="build", stage="implement", issue="#7",
                              label="Fix pipeline", kind="open", item="13")
        cfg = backlog.Backlog("https://labs.example.test", "team/app")
        issue = backlog.Issue(7, "Build it", "opened", ("03 - BUILD",))
        with mock.patch.object(audit_tasks, "issue_states", return_value={7: issue}):
            self.assertEqual(audit_tasks.kanban_faults([feature, fix], cfg, FLOW), [])

    def test_rows_sharing_an_issue_drift_against_the_furthest_stage(self):
        """#87: one drift fault per issue, naming the furthest row — never one per row."""
        feature = SimpleNamespace(standing=False, lane="build", stage="plan", issue="#7",
                                  label="Feature", kind="open", item="12")
        fix = SimpleNamespace(standing=False, lane="build", stage="implement", issue="#7",
                              label="Fix pipeline", kind="open", item="13")
        cfg = backlog.Backlog("https://labs.example.test", "team/app")
        issue = backlog.Issue(7, "Build it", "opened", ("04 - REVIEW",))
        with mock.patch.object(audit_tasks, "issue_states", return_value={7: issue}):
            faults = audit_tasks.kanban_faults([feature, fix], cfg, FLOW)
        self.assertEqual(len(faults), 1, [str(f) for f in faults])
        self.assertIn("expected 03 - BUILD", str(faults[0]))
        self.assertIn("Fix pipeline", str(faults[0]))
        self.assertNotIn("Feature", str(faults[0]))

    def test_a_closed_issue_with_a_stage_label_expects_no_managed_labels(self):
        """#87: a closed issue keeps no managed stage label; the audit says so instead of expecting the row's stage."""
        task = SimpleNamespace(standing=False, lane="build", stage="implement", issue="#7",
                               label="Build it", kind="open", item="Build it")
        cfg = backlog.Backlog("https://labs.example.test", "team/app")
        issue = backlog.Issue(7, "Build it", "closed", ("03 - BUILD",))
        with mock.patch.object(audit_tasks, "issue_states", return_value={7: issue}):
            faults = audit_tasks.kanban_faults([task], cfg, FLOW)
        self.assertEqual(len(faults), 1, [str(f) for f in faults])
        self.assertIn("expected no managed labels, found 03 - BUILD", str(faults[0]))

    def test_a_closed_issue_without_managed_labels_is_clean(self):
        task = SimpleNamespace(standing=False, lane="build", stage="implement", issue="#7",
                               label="Build it", kind="open", item="Build it")
        cfg = backlog.Backlog("https://labs.example.test", "team/app")
        issue = backlog.Issue(7, "Build it", "closed", ("kind::code",))
        with mock.patch.object(audit_tasks, "issue_states", return_value={7: issue}):
            self.assertEqual(audit_tasks.kanban_faults([task], cfg, FLOW), [])


if __name__ == "__main__":
    unittest.main()
